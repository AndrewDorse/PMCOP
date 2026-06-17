from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from py_clob_client_v2 import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)
from rich.console import Console

from .config import Settings
from .utils import load_json, save_json

try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone(timedelta(hours=-4))

CLEAR_CACHE_ON_RUN_START = False
BALANCE_REFRESH_SECONDS = int(os.getenv("BALANCE_REFRESH_SECONDS", "10"))
ACTIVITY_ALLOWED_ASSETS = [
    x.strip().lower()
    for x in os.getenv("ACTIVITY_ALLOWED_ASSETS", "btc,sol,bnb,eth").split(",")
    if x.strip()
]
ACTIVITY_ALLOWED_TITLE_KEYWORDS = [
    x.strip().lower()
    for x in os.getenv("ACTIVITY_ALLOWED_TITLE_KEYWORDS", "").split(",")
    if x.strip()
]
ACTIVITY_REQUIRED_WINDOW_MINUTES = int(os.getenv("ACTIVITY_REQUIRED_WINDOW_MINUTES", "5"))
COPY_SOURCE_USDC_MULTIPLIER = float(os.getenv("COPY_SOURCE_USDC_MULTIPLIER", "0.05"))
MIN_COPY_USDC = float(os.getenv("MIN_COPY_USDC", "1.0"))
MIN_COPY_SHARES = float(os.getenv("MIN_COPY_SHARES", "5.0"))
MAX_COPY_USDC = float(os.getenv("MAX_COPY_USDC", "10.0"))
MAX_COPY_USDC_PER_WINDOW = float(os.getenv("MAX_COPY_USDC_PER_WINDOW", "25.0"))
MAX_COPY_USDC_PER_CYCLE = float(os.getenv("MAX_COPY_USDC_PER_CYCLE", "50.0"))
MAX_COPY_USDC_PER_DAY = float(os.getenv("MAX_COPY_USDC_PER_DAY", "150.0"))
MAX_COPY_ASK_PRICE = float(os.getenv("MAX_COPY_ASK_PRICE", "0.98"))
ACTIVITY_BUY_SLIPPAGE_BPS = int(os.getenv("ACTIVITY_BUY_SLIPPAGE_BPS", "150"))

DEBUG_LOG_FILENAME = "activity_runner.log"
COPIED_LEDGER_FILENAME = "copied_deals_ledger.jsonl"
WINDOW_PNL_FILENAME = "copied_window_pnl.csv"
PAIR_PNL_FILENAME = "copied_pair_pnl.csv"
SLUG_RE = re.compile(r"^(?P<pair>[a-z]+)-updown-5m-\d+$", re.IGNORECASE)


def _norm_tick(raw: str | None) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    return s if s in {"0.1", "0.01", "0.001", "0.0001"} else None


@dataclass
class ActivityTrade:
    wallet: str
    asset: str
    title: str
    outcome: str
    side: str
    size: float
    price: float
    timestamp: int
    amount: float
    tx_hash: str
    condition_id: str = ""
    event_slug: str = ""
    activity_type: str = ""
    raw_id: str = ""

    @property
    def dedupe_key(self) -> str:
        tx = (self.tx_hash or "").strip().lower()
        if tx:
            return f"tx:{tx}:{self.asset}:{self.side.upper()}:{self.outcome.strip().lower()}"
        return "|".join(
            [
                self.wallet.lower(),
                self.asset,
                self.side.upper(),
                self.outcome.strip().lower(),
                f"{self.size:.8f}",
                f"{self.price:.8f}",
                str(self.timestamp),
                self.raw_id or "noid",
            ]
        )


class PublicActivityApi:
    def __init__(self, timeout: int = 20):
        self.session = requests.Session()
        self.base = os.getenv("PM_DATA_API_BASE", "https://data-api.polymarket.com").rstrip("/")
        api_key = os.getenv("PM_DATA_API_KEY", "").strip()
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_recent_activity(self, wallet: str, limit: int = 50) -> List[dict]:
        # Polymarket Data API only accepts `user` (see docs); other params return 400.
        try:
            data = self._get(
                "/activity",
                {"user": wallet, "limit": limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            )
        except Exception:
            return []
        items: List[dict] = []
        if isinstance(data, list):
            items.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            for key in ("activity", "data", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    items.extend(x for x in value if isinstance(x, dict))
        return items


class MarketActivityTracker:
    WINDOW_RE = re.compile(
        r"(?P<start>\d{1,2}:\d{2}\s*[ap]m)\s*-\s*(?P<end>\d{1,2}:\d{2}\s*[ap]m)\s*et",
        re.IGNORECASE,
    )
    MONTH_DAY_RE = re.compile(r"\b(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\b", re.IGNORECASE)

    def __init__(self, settings: Settings, console: Console | None = None):
        self.settings = settings
        self.console = console or Console()
        self.api = PublicActivityApi()
        self.cache_file = settings.state_dir / "activity_cache.json"
        self.log_file = settings.state_dir / DEBUG_LOG_FILENAME
        self.ledger_file = settings.state_dir / COPIED_LEDGER_FILENAME
        self.settings.prepare_dirs()

        self.client = ClobClient(
            settings.pm_host,
            chain_id=settings.pm_chain_id,
            key=settings.pm_private_key,
            signature_type=settings.pm_signature_type,
            funder=settings.pm_funder,
        )
        rk = (settings.pm_relayer_api_key or "").strip()
        if rk:
            self.client.set_api_creds(
                ApiCreds(
                    api_key=rk,
                    api_secret=(settings.pm_relayer_secret or "").strip(),
                    api_passphrase=(settings.pm_relayer_passphrase or "").strip(),
                )
            )
            self.auth_mode = "external_api_creds"
        else:
            creds = self.client.derive_api_key()
            if creds is None:
                creds = self.client.create_api_key(int(time.time() * 1000))
            self.client.set_api_creds(creds)
            self.auth_mode = "derived_from_private_key"
        self.cached_available_balance = 0.0
        self.cached_balance_ts = 0.0

        self.logger = logging.getLogger(f"activity_runner_{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        fh = logging.FileHandler(self.log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(fh)

        try:
            self.client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=settings.pm_signature_type,
                )
            )
        except Exception as exc:
            self._debug("Collateral allowance sync", {"error": str(exc)})

    def clear_cache(self) -> None:
        save_json(
            self.cache_file,
            {
                "seen": [],
                "repeated": [],
                "forbidden_buy_markets": {},
                "market_outcome_entries": {},
                "asset_last_buy_ts": {},
                "copied_deals": {},
            },
        )

    def _load_cache(self) -> dict:
        return load_json(
            self.cache_file,
            {
                "seen": [],
                "repeated": [],
                "forbidden_buy_markets": {},
                "market_outcome_entries": {},
                "asset_last_buy_ts": {},
                "copied_deals": {},
            },
        )

    def _save_cache(self, cache: dict) -> None:
        cache["seen"] = list(cache.get("seen", []))[-10000:]
        cache["repeated"] = list(cache.get("repeated", []))[-3000:]
        forbidden = cache.get("forbidden_buy_markets", {})
        cache["forbidden_buy_markets"] = forbidden if isinstance(forbidden, dict) else {}
        entries = cache.get("market_outcome_entries", {})
        cache["market_outcome_entries"] = entries if isinstance(entries, dict) else {}
        alt = cache.get("asset_last_buy_ts", {})
        cache["asset_last_buy_ts"] = alt if isinstance(alt, dict) else {}
        copied = cache.get("copied_deals", {})
        cache["copied_deals"] = copied if isinstance(copied, dict) else {}
        save_json(self.cache_file, cache)

    def _market_buy_key(self, title: str, outcome: str) -> str:
        return f"{title.strip().lower()}::{outcome.strip().lower()}"

    def _prune_forbidden_buy_markets(self, cache: dict) -> None:
        forbidden = cache.get("forbidden_buy_markets", {})
        if not isinstance(forbidden, dict):
            cache["forbidden_buy_markets"] = {}
        else:
            keep = {}
            for key, payload in forbidden.items():
                title = ""
                if isinstance(payload, dict):
                    title = str(payload.get("title") or "")
                if title and self._is_active_title(title):
                    keep[key] = payload
            cache["forbidden_buy_markets"] = keep

        entries = cache.get("market_outcome_entries", {})
        if not isinstance(entries, dict):
            cache["market_outcome_entries"] = {}
        else:
            keep_entries = {}
            for key, payload in entries.items():
                title = ""
                if isinstance(payload, dict):
                    title = str(payload.get("title") or "")
                if title and self._is_active_title(title):
                    keep_entries[key] = payload
            cache["market_outcome_entries"] = keep_entries

        copied = cache.get("copied_deals", {})
        cache["copied_deals"] = copied if isinstance(copied, dict) else {}

    def _debug(self, message: str, payload: Optional[dict] = None) -> None:
        if payload is None:
            self.logger.info(message)
        else:
            self.logger.info("%s | %s", message, json.dumps(payload, ensure_ascii=False))

    def _log_copy_deal(self, trade: ActivityTrade, *, copied_usd: float, dry_run: bool, response: Any = None) -> None:
        payload = {
            "event": "COPY_DEAL",
            "dry_run": dry_run,
            "source_wallet": trade.wallet,
            "title": trade.title,
            "outcome": trade.outcome,
            "asset": trade.asset,
            "event_slug": trade.event_slug,
            "condition_id": trade.condition_id,
            "copied_usd": copied_usd,
            "source_tx": trade.tx_hash or None,
            "source_key": trade.dedupe_key,
        }
        if response is not None and not dry_run:
            try:
                payload["response"] = self._to_serializable_response(response)
            except Exception:
                payload["response"] = str(response)[:2000]
        self.logger.info("COPY_DEAL | %s", json.dumps(payload, ensure_ascii=False))

    def _to_serializable_response(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            try:
                return json.loads(json.dumps(value, default=str)[:4000])
            except Exception:
                return str(value)[:2000]
        return str(value)[:2000]

    def _log_error(self, event: str, message: str, *, payload: Optional[dict] = None, exc: Optional[BaseException] = None) -> None:
        extra = dict(payload or {})
        extra["event"] = event
        extra["message"] = message
        line = f"{event} | {json.dumps(extra, ensure_ascii=False)}"
        if exc is not None:
            self.logger.error(
                "%s | %s: %s",
                line,
                type(exc).__name__,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            self.logger.error("%s", line)

    def _compact(self, message: str) -> None:
        self.console.print(message)

    def _append_ledger(self, row: dict) -> None:
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str))
            f.write("\n")

    def _read_ledger(self) -> List[dict]:
        if not self.ledger_file.exists():
            return []
        rows: List[dict] = []
        with open(self.ledger_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
        return rows

    def _today_copied_usdc(self) -> float:
        now = datetime.now(timezone.utc).date()
        total = 0.0
        for row in self._read_ledger():
            ts = self._to_int(row.get("copied_at"))
            if ts <= 0:
                continue
            if datetime.fromtimestamp(ts, tz=timezone.utc).date() != now:
                continue
            status = str(row.get("order_status") or "").lower()
            if status in {"skipped_cap", "skipped_ask"}:
                continue
            total += self._to_float(row.get("copy_usdc") or row.get("copied_usdc"))
        return total

    def load_watch_wallets(self) -> List[str]:
        data = load_json(self.settings.watchlist_file, {"wallets": []})
        wallets = [str(w).lower() for w in data.get("wallets", []) if str(w).strip()]
        return sorted(set(wallets))

    def _to_float(self, value: Any, default: float = 0.0) -> float:
        if value is None or isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return default
        return default

    def _to_int(self, value: Any, default: int = 0) -> int:
        if value is None or isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                return default
        return default

    def _extract_price_level(self, level: Any) -> float:
        if isinstance(level, dict):
            for key in ("price", "p", "px"):
                price = self._to_float(level.get(key), 0.0)
                if price > 0:
                    return price
        elif isinstance(level, (list, tuple)) and level:
            return self._to_float(level[0], 0.0)
        return 0.0

    def _best_ask(self, token_id: str) -> float:
        book = self.client.get_order_book(token_id)
        asks = []
        if isinstance(book, dict):
            asks = book.get("asks") or book.get("sell") or []
        else:
            asks = getattr(book, "asks", None) or getattr(book, "sell", None) or []
        prices = [self._extract_price_level(level) for level in asks]
        prices = [price for price in prices if price > 0]
        if not prices:
            raise RuntimeError(f"no usable ask price for token {token_id}")
        return min(prices)

    def _normalize_side(self, raw: dict) -> str:
        for candidate in (
            raw.get("side"),
            raw.get("tradeType"),
            raw.get("activityType"),
            raw.get("orderSide"),
            raw.get("takerSide"),
            raw.get("action"),
            raw.get("verb"),
        ):
            value = str(candidate or "").strip().upper()
            if value in {"BUY", "SELL", "REDEEM"}:
                return value
        return ""

    def _parse_ampm_minutes(self, raw: str) -> Optional[int]:
        try:
            dt = datetime.strptime(raw.strip().upper().replace(" ", ""), "%I:%M%p")
            return dt.hour * 60 + dt.minute
        except Exception:
            return None

    def _month_to_num(self, raw: str) -> Optional[int]:
        try:
            return datetime.strptime(raw[:3], "%b").month
        except Exception:
            return None

    def _now_et(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(ET_TZ)

    def _window_minutes_from_title(self, title: str) -> Optional[int]:
        match = self.WINDOW_RE.search(title)
        if not match:
            return None
        start_minutes = self._parse_ampm_minutes(match.group("start"))
        end_minutes = self._parse_ampm_minutes(match.group("end"))
        if start_minutes is None or end_minutes is None:
            return None
        diff = end_minutes - start_minutes
        if diff < 0:
            diff += 24 * 60
        return diff

    def _market_end_et_from_title(self, title: str) -> Optional[datetime]:
        match = self.WINDOW_RE.search(title)
        if not match:
            return None
        end_minutes = self._parse_ampm_minutes(match.group("end"))
        if end_minutes is None:
            return None
        md = self.MONTH_DAY_RE.search(title)
        if not md:
            return None
        month = self._month_to_num(md.group("month"))
        day = int(md.group("day"))
        if month is None:
            return None
        now_et = self._now_et()
        return now_et.replace(month=month, day=day, hour=end_minutes // 60, minute=end_minutes % 60, second=0, microsecond=0)

    def _window_key(self, trade: ActivityTrade) -> str:
        slug = (trade.event_slug or "").strip().lower()
        if slug:
            return slug
        condition = (trade.condition_id or "").strip().lower()
        if condition:
            return condition
        return re.sub(r"\s+", " ", trade.title.strip().lower())

    def _is_deal_copied(self, cache: dict, trade: ActivityTrade) -> bool:
        copied = cache.get("copied_deals", {})
        if not isinstance(copied, dict):
            cache["copied_deals"] = {}
            return False
        return trade.dedupe_key in copied

    def _register_copied_deal(self, cache: dict, trade: ActivityTrade, *, copied_usd: float, result_ok: bool) -> None:
        copied = cache.get("copied_deals", {})
        copied_deals = dict(copied) if isinstance(copied, dict) else {}
        copied_deals[trade.dedupe_key] = {
            "copied_at": int(time.time()),
            "source_wallet": trade.wallet,
            "event_slug": trade.event_slug,
            "condition_id": trade.condition_id,
            "outcome": trade.outcome,
            "asset": trade.asset,
            "source_usdc": trade.amount,
            "copied_usdc": copied_usd,
            "source_price": trade.price,
            "source_size": trade.size,
            "result_ok": result_ok,
        }
        cache["copied_deals"] = copied_deals

    def _is_fresh_trade(self, trade: ActivityTrade) -> bool:
        max_age = int(os.getenv("ACTIVITY_MAX_AGE_SECONDS", "3600"))
        return abs(int(time.time()) - trade.timestamp) <= max_age

    def _is_active_title(self, title: str) -> bool:
        end_dt = self._market_end_et_from_title(title)
        if end_dt is None:
            return True
        return end_dt > self._now_et()

    def _pair_from_slug(self, slug: str) -> str:
        match = SLUG_RE.match((slug or "").strip().lower())
        return match.group("pair").lower() if match else ""

    def _pair_from_title(self, title: str) -> str:
        title_lc = title.lower()
        for pair in ACTIVITY_ALLOWED_ASSETS:
            aliases = {
                "btc": ("btc", "bitcoin"),
                "eth": ("eth", "ethereum"),
                "sol": ("sol", "solana"),
                "bnb": ("bnb", "binance"),
            }.get(pair, (pair,))
            if any(alias in title_lc for alias in aliases):
                return pair
        return ""

    def _is_allowed_market(self, trade: ActivityTrade) -> bool:
        pair = self._pair_from_slug(trade.event_slug)
        if pair:
            return pair in ACTIVITY_ALLOWED_ASSETS
        title_lc = trade.title.lower()
        if ACTIVITY_ALLOWED_TITLE_KEYWORDS and not any(keyword in title_lc for keyword in ACTIVITY_ALLOWED_TITLE_KEYWORDS):
            return False
        if ACTIVITY_ALLOWED_ASSETS and not self._pair_from_title(trade.title):
            return False
        if ACTIVITY_REQUIRED_WINDOW_MINUTES > 0:
            window_minutes = self._window_minutes_from_title(trade.title)
            if window_minutes != ACTIVITY_REQUIRED_WINDOW_MINUTES:
                return False
        return True

    def _extract_trade(self, wallet: str, raw: dict) -> Optional[ActivityTrade]:
        side = self._normalize_side(raw)
        if side != "BUY":
            return None
        title = str(raw.get("title") or raw.get("market") or raw.get("marketTitle") or raw.get("question") or raw.get("eventTitle") or raw.get("event") or "").strip()
        outcome = str(raw.get("outcome") or raw.get("outcomeName") or raw.get("outcome_name") or "").strip()
        asset = str(raw.get("asset") or raw.get("tokenID") or raw.get("tokenId") or raw.get("token_id") or raw.get("outcomeToken") or raw.get("asset_id") or "").strip()
        condition_id = str(raw.get("conditionId") or raw.get("condition_id") or raw.get("conditionID") or raw.get("marketConditionId") or raw.get("market_condition_id") or "").strip()
        event_slug = str(raw.get("eventSlug") or raw.get("event_slug") or raw.get("slug") or raw.get("marketSlug") or raw.get("market_slug") or raw.get("event_slug") or "").strip()
        activity_type = str(raw.get("activityType") or raw.get("type") or raw.get("action") or "").strip().upper()
        tx_hash = str(raw.get("transactionHash") or raw.get("transaction_hash") or raw.get("txHash") or raw.get("hash") or raw.get("transactionHashHex") or "").strip()
        raw_id = str(raw.get("id") or raw.get("tradeID") or raw.get("tradeId") or raw.get("orderID") or raw.get("orderId") or "").strip()
        size = self._to_float(raw.get("size") or raw.get("shares") or raw.get("amount"))
        price = self._to_float(raw.get("price") or raw.get("rate"))
        timestamp = self._to_int(raw.get("timestamp") or raw.get("time") or raw.get("createdAt") or raw.get("created_at"))
        if timestamp > 10**12:
            timestamp = timestamp // 1000
        if price <= 0:
            price = self._to_float(raw.get("avgPrice") or raw.get("avg_price") or raw.get("fillPrice"))
        if not title or not asset or size <= 0 or price <= 0 or timestamp <= 0:
            return None
        return ActivityTrade(
            wallet=wallet,
            asset=asset,
            title=title,
            outcome=outcome,
            side=side,
            size=size,
            price=price,
            timestamp=timestamp,
            amount=size * price,
            tx_hash=tx_hash,
            condition_id=condition_id,
            event_slug=event_slug,
            activity_type=activity_type,
            raw_id=raw_id,
        )

    def fetch_recent_wallet_activity(self, wallet: str, limit: int = 50, multiplier: int = 1) -> List[ActivityTrade]:
        request_limit = max(limit * max(multiplier, 1), limit)
        raw_activity = self.api.get_recent_activity(wallet, limit=request_limit)
        items: List[ActivityTrade] = []
        seen_ids: set[str] = set()
        for item in raw_activity:
            if not isinstance(item, dict):
                continue
            trade = self._extract_trade(wallet, item)
            if trade is None:
                continue
            key = trade.dedupe_key
            if key in seen_ids:
                continue
            seen_ids.add(key)
            items.append(trade)
        items.sort(key=lambda x: x.timestamp, reverse=True)
        return items[:request_limit]

    def _parse_usdc_collateral_balance(self, resp: Any) -> float:
        """Match KNG4 prst1 ``_parse_balance_allowance``: micro-units vs human float."""
        if isinstance(resp, dict):
            raw = resp.get("balance")
            if raw is None and isinstance(resp.get("data"), dict):
                raw = resp["data"].get("balance")
        else:
            raw = getattr(resp, "balance", None) or resp
        if raw is None or raw == "":
            return 0.0
        if isinstance(raw, str):
            raw = raw.strip()
            if re.fullmatch(r"\d+", raw):
                return float(raw) / 1_000_000.0
        elif isinstance(raw, int):
            return float(raw) / 1_000_000.0
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return 0.0
        if v >= 1000 and float(v).is_integer():
            return v / 1_000_000.0
        return v

    def get_available_usdc_balance(self) -> float:
        try:
            payload = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=self.settings.pm_signature_type,
                )
            )
            return max(0.0, self._parse_usdc_collateral_balance(payload))
        except Exception as exc:
            self._debug("get_balance_allowance", {"error": str(exc)})
            return max(0.0, self.cached_available_balance)

    def get_cached_available_usdc_balance(self) -> float:
        now_ts = time.time()
        if now_ts - self.cached_balance_ts >= BALANCE_REFRESH_SECONDS:
            self.cached_available_balance = self.get_available_usdc_balance()
            self.cached_balance_ts = now_ts
            self._debug("Balance refreshed", {"available_usdc": self.cached_available_balance})
        return self.cached_available_balance

    def _copy_usdc_for_trade(self, trade: ActivityTrade) -> float:
        raw = trade.amount * COPY_SOURCE_USDC_MULTIPLIER
        clipped = min(max(raw, MIN_COPY_USDC), MAX_COPY_USDC)
        return int(clipped * 100) / 100.0

    def _partial_market_options(self, token_id: str) -> PartialCreateOrderOptions | None:
        tick = None
        neg = None
        try:
            tick = _norm_tick(self.client.get_tick_size(token_id))
        except Exception:
            pass
        try:
            neg = bool(self.client.get_neg_risk(token_id))
        except Exception:
            pass
        if tick is None and neg is None:
            self._debug("CLOB market options omitted", {"token_id": token_id[:24]})
            return None
        return PartialCreateOrderOptions(
            tick_size=tick,
            neg_risk=bool(neg) if neg is not None else None,
        )

    def _build_limit_order(self, trade: ActivityTrade, shares: float, limit_price: float) -> OrderArgs:
        return OrderArgs(
            token_id=trade.asset,
            price=float(limit_price),
            size=float(shares),
            side=Side.BUY,
        )

    def _copy_order_plan(self, trade: ActivityTrade) -> dict:
        target_usdc = self._copy_usdc_for_trade(trade)
        available_balance = self.get_cached_available_usdc_balance()
        best_ask = self._best_ask(trade.asset)
        if best_ask > MAX_COPY_ASK_PRICE:
            return {
                "ok": False,
                "message": f"skipped: best ask {best_ask:.4f} > max {MAX_COPY_ASK_PRICE:.4f}",
                "copied_usd": 0.0,
                "shares": 0.0,
                "best_ask": best_ask,
            }
        limit_price = min(0.99, best_ask * (1 + ACTIVITY_BUY_SLIPPAGE_BPS / 10000.0))
        if available_balance < MIN_COPY_SHARES * limit_price:
            return {
                "ok": False,
                "message": f"skipped: balance {available_balance:.2f} cannot buy min {MIN_COPY_SHARES:g} shares",
                "copied_usd": 0.0,
                "shares": 0.0,
                "best_ask": best_ask,
                "limit_price": limit_price,
            }

        desired_shares = max(MIN_COPY_SHARES, target_usdc / limit_price)
        max_balance_shares = available_balance / limit_price
        shares = min(desired_shares, max_balance_shares)
        shares = int(shares * 100) / 100.0
        if shares < MIN_COPY_SHARES:
            return {
                "ok": False,
                "message": f"skipped: balance {available_balance:.2f} cannot buy min {MIN_COPY_SHARES:g} shares",
                "copied_usd": 0.0,
                "shares": 0.0,
                "best_ask": best_ask,
                "limit_price": limit_price,
            }
        copy_usdc = int(shares * limit_price * 100) / 100.0
        return {
            "ok": True,
            "target_usdc": target_usdc,
            "copied_usd": copy_usdc,
            "shares": shares,
            "best_ask": best_ask,
            "limit_price": limit_price,
            "available_balance": available_balance,
        }

    def repeat_trade(self, trade: ActivityTrade, plan: dict) -> dict:
        opts = self._partial_market_options(trade.asset)
        shares = float(plan["shares"])
        limit_price = float(plan["limit_price"])
        copied_usd = float(plan["copied_usd"])
        order_args = self._build_limit_order(trade, shares, limit_price)
        if self.settings.dry_run:
            return {
                "ok": True,
                "message": f"DRY_RUN would place limit BUY (GTC) for {trade.title} / {trade.outcome}",
                "copied_usd": copied_usd,
                "shares": shares,
                "best_ask": plan.get("best_ask"),
                "limit_price": limit_price,
                "order_args": {
                    "token_id": order_args.token_id,
                    "size": order_args.size,
                    "side": str(order_args.side),
                    "price": order_args.price,
                    "order_type": str(OrderType.GTC),
                    "partial_options": str(opts) if opts else None,
                },
            }
        signed_order = self.client.create_order(order_args, options=opts)
        resp = self.client.post_order(signed_order, order_type=OrderType.GTC)
        return {
            "ok": True,
            "message": f"placed limit BUY (GTC) for {trade.title} / {trade.outcome}",
            "copied_usd": copied_usd,
            "shares": shares,
            "best_ask": plan.get("best_ask"),
            "limit_price": limit_price,
            "response": resp,
        }

    def _ledger_row(
        self,
        trade: ActivityTrade,
        copy_usdc: float,
        order_status: str,
        *,
        message: str = "",
        best_ask: Any = None,
        limit_price: Any = None,
        shares: Any = None,
        response: Any = None,
    ) -> dict:
        row = {
            "copied_at": int(time.time()),
            "source_wallet": trade.wallet,
            "source_tx": trade.tx_hash,
            "source_key": trade.dedupe_key,
            "event_slug": trade.event_slug,
            "condition_id": trade.condition_id,
            "token_id": trade.asset,
            "title": trade.title,
            "outcome": trade.outcome,
            "source_usdc": trade.amount,
            "source_price": trade.price,
            "source_size": trade.size,
            "copy_usdc": copy_usdc,
            "copy_shares": shares,
            "order_status": order_status,
            "message": message,
            "best_ask": best_ask,
            "limit_price": limit_price,
        }
        if response is not None:
            row["response"] = self._to_serializable_response(response)
        return row

    def _activity_condition_id(self, raw: dict) -> str:
        return str(raw.get("conditionId") or raw.get("condition_id") or raw.get("conditionID") or raw.get("marketConditionId") or raw.get("market_condition_id") or "").strip()

    def _activity_slug(self, raw: dict) -> str:
        return str(raw.get("eventSlug") or raw.get("event_slug") or raw.get("slug") or raw.get("marketSlug") or raw.get("market_slug") or "").strip()

    def _activity_amount_usdc(self, raw: dict) -> float:
        amount = self._to_float(raw.get("amount") or raw.get("usdcAmount") or raw.get("usdc_amount") or raw.get("value"))
        if amount > 0:
            return amount
        size = self._to_float(raw.get("size") or raw.get("shares"))
        price = self._to_float(raw.get("price") or raw.get("rate") or raw.get("avgPrice") or raw.get("avg_price"))
        return max(0.0, size * price)

    def _pair_from_row(self, row: dict) -> str:
        slug = str(row.get("event_slug") or "")
        pair = self._pair_from_slug(slug)
        if pair:
            return pair.upper()
        title = str(row.get("title") or "")
        pair = self._pair_from_title(title)
        return pair.upper() if pair else "UNKNOWN"

    def write_pnl_reports(self, limit: int = 500) -> None:
        ledger_rows = [row for row in self._read_ledger() if self._to_float(row.get("copy_usdc")) > 0]
        if not ledger_rows:
            self._compact("[yellow]PNL[/yellow] no copied ledger rows")
            return

        own_wallet = (self.settings.pm_funder or "").strip().lower()
        raw_activity = self.api.get_recent_activity(own_wallet, limit=limit) if own_wallet else []
        redeem_by_condition: dict[str, float] = defaultdict(float)
        for item in raw_activity:
            if not isinstance(item, dict):
                continue
            side = self._normalize_side(item)
            if side != "REDEEM":
                continue
            condition_id = self._activity_condition_id(item)
            if not condition_id:
                continue
            redeem_by_condition[condition_id] += self._activity_amount_usdc(item)

        by_condition: dict[str, dict] = {}
        for row in ledger_rows:
            condition_id = str(row.get("condition_id") or "").strip()
            if not condition_id:
                continue
            bucket = by_condition.setdefault(
                condition_id,
                {
                    "condition_id": condition_id,
                    "event_slug": row.get("event_slug") or "",
                    "title": row.get("title") or "",
                    "pair": self._pair_from_row(row),
                    "buy_cost": 0.0,
                    "redeem_value": 0.0,
                    "status": "open",
                },
            )
            bucket["buy_cost"] += self._to_float(row.get("copy_usdc"))

        for condition_id, bucket in by_condition.items():
            redeem_value = redeem_by_condition.get(condition_id, 0.0)
            bucket["redeem_value"] = redeem_value
            if redeem_value > 0:
                bucket["status"] = "redeemed"
            buy_cost = bucket["buy_cost"]
            pnl = redeem_value - buy_cost if redeem_value > 0 else 0.0
            bucket["realized_pnl"] = pnl
            bucket["roi"] = pnl / buy_cost if redeem_value > 0 and buy_cost > 0 else 0.0

        window_path = self.settings.state_dir / WINDOW_PNL_FILENAME
        pair_path = self.settings.state_dir / PAIR_PNL_FILENAME
        with open(window_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["condition_id", "event_slug", "title", "pair", "status", "buy_cost", "redeem_value", "realized_pnl", "roi"])
            writer.writeheader()
            for row in by_condition.values():
                writer.writerow(row)

        pair_summary: dict[str, dict] = {}
        for row in by_condition.values():
            pair = row["pair"]
            bucket = pair_summary.setdefault(pair, {"pair": pair, "windows": 0, "redeemed_windows": 0, "open_windows": 0, "buy_cost": 0.0, "redeem_value": 0.0, "realized_pnl": 0.0, "roi": 0.0})
            bucket["windows"] += 1
            bucket["buy_cost"] += row["buy_cost"]
            bucket["redeem_value"] += row["redeem_value"]
            bucket["realized_pnl"] += row["realized_pnl"]
            if row["status"] == "redeemed":
                bucket["redeemed_windows"] += 1
            else:
                bucket["open_windows"] += 1
        for bucket in pair_summary.values():
            bucket["roi"] = bucket["realized_pnl"] / bucket["buy_cost"] if bucket["buy_cost"] > 0 else 0.0
        with open(pair_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["pair", "windows", "redeemed_windows", "open_windows", "buy_cost", "redeem_value", "realized_pnl", "roi"])
            writer.writeheader()
            for row in pair_summary.values():
                writer.writerow(row)

        realized = sum(row["realized_pnl"] for row in by_condition.values())
        open_windows = sum(1 for row in by_condition.values() if row["status"] == "open")
        self._compact(f"[cyan]PNL[/cyan] wrote {window_path} and {pair_path}; realized=${realized:.2f}; open_windows={open_windows}")

    def cycle(self, limit: int = 50, activity_fetch_multiplier: int = 1) -> None:
        wallets = self.load_watch_wallets()
        if not wallets:
            raise RuntimeError("Watch list is empty. Add at least one wallet first.")

        cache = self._load_cache()
        self._prune_forbidden_buy_markets(cache)
        seen = set(str(x) for x in cache.get("seen", []))
        repeated = list(cache.get("repeated", []))
        copied_deals = cache.get("copied_deals", {})
        if not isinstance(copied_deals, dict):
            copied_deals = {}
            cache["copied_deals"] = copied_deals
        window_spend: dict[str, float] = defaultdict(float)
        cycle_spend = 0.0
        day_spend = self._today_copied_usdc()
        candidates: List[ActivityTrade] = []

        skip_ended_title = os.getenv("ACTIVITY_SKIP_ENDED_TITLE_FILTER", "true").lower() in {"1", "true", "yes", "y", "on"}

        for wallet in wallets:
            trades = self.fetch_recent_wallet_activity(wallet, limit=limit, multiplier=activity_fetch_multiplier)
            skipped_seen = skipped_fresh = skipped_title = 0
            skipped_market = 0
            for trade in trades:
                if trade.dedupe_key in seen or trade.dedupe_key in copied_deals:
                    skipped_seen += 1
                    continue
                if not self._is_fresh_trade(trade):
                    skipped_fresh += 1
                    continue
                if skip_ended_title and not self._is_active_title(trade.title):
                    skipped_title += 1
                    continue
                if not self._is_allowed_market(trade):
                    skipped_market += 1
                    continue
                candidates.append(trade)
            self._debug(
                "Activity scan",
                {
                    "wallet": wallet,
                    "parsed_buy_rows": len(trades),
                    "activity_fetch_limit": limit * max(activity_fetch_multiplier, 1),
                    "skipped_seen": skipped_seen,
                    "skipped_fresh": skipped_fresh,
                    "skipped_ended_title": skipped_title,
                    "skipped_market_filter": skipped_market,
                    "allowed_assets": ACTIVITY_ALLOWED_ASSETS,
                    "allowed_title_keywords": ACTIVITY_ALLOWED_TITLE_KEYWORDS,
                    "required_window_minutes": ACTIVITY_REQUIRED_WINDOW_MINUTES,
                    "skip_ended_title_filter": skip_ended_title,
                },
            )

        candidates.sort(key=lambda x: x.timestamp)

        if not candidates:
            self._save_cache(cache)
            return

        for trade in candidates:
            if self._is_deal_copied(cache, trade):
                continue

            window_key = self._window_key(trade)
            try:
                plan = self._copy_order_plan(trade)
            except Exception as exc:
                message = f"error: {exc}"
                self._compact(f"[red]COPY_ORDER_FAILED[/red] {trade.title} | {exc}")
                self._append_ledger(self._ledger_row(trade, 0.0, "failed", message=message))
                self._register_copied_deal(cache, trade, copied_usd=0.0, result_ok=False)
                self._log_error(
                    "COPY_ORDER_FAILED",
                    str(exc),
                    payload={
                        "source_wallet": trade.wallet,
                        "title": trade.title,
                        "outcome": trade.outcome,
                        "asset": trade.asset,
                    },
                    exc=exc,
                )
                continue
            copy_buy_usd = float(plan.get("copied_usd", 0.0))
            if not bool(plan.get("ok")):
                message = str(plan.get("message") or "skipped")
                self._compact(f"[yellow]COPY_SKIPPED[/yellow] {trade.title} | {message}")
                self._append_ledger(
                    self._ledger_row(
                        trade,
                        copy_buy_usd,
                        "skipped_ask" if "best ask" in message else "skipped_balance",
                        message=message,
                        best_ask=plan.get("best_ask"),
                        limit_price=plan.get("limit_price"),
                        shares=plan.get("shares"),
                    )
                )
                self._register_copied_deal(cache, trade, copied_usd=0.0, result_ok=False)
                continue
            if window_spend[window_key] + copy_buy_usd > MAX_COPY_USDC_PER_WINDOW:
                message = "skipped: window cap"
                self._compact(f"[yellow]COPY_SKIPPED[/yellow] {trade.title} | {message}")
                self._append_ledger(self._ledger_row(trade, copy_buy_usd, "skipped_cap", message=message, best_ask=plan.get("best_ask"), limit_price=plan.get("limit_price"), shares=plan.get("shares")))
                self._register_copied_deal(cache, trade, copied_usd=0.0, result_ok=False)
                continue
            if cycle_spend + copy_buy_usd > MAX_COPY_USDC_PER_CYCLE:
                message = "skipped: cycle cap"
                self._compact(f"[yellow]COPY_SKIPPED[/yellow] {trade.title} | {message}")
                self._append_ledger(self._ledger_row(trade, copy_buy_usd, "skipped_cap", message=message, best_ask=plan.get("best_ask"), limit_price=plan.get("limit_price"), shares=plan.get("shares")))
                self._register_copied_deal(cache, trade, copied_usd=0.0, result_ok=False)
                continue
            if day_spend + copy_buy_usd > MAX_COPY_USDC_PER_DAY:
                message = "skipped: day cap"
                self._compact(f"[yellow]COPY_SKIPPED[/yellow] {trade.title} | {message}")
                self._append_ledger(self._ledger_row(trade, copy_buy_usd, "skipped_cap", message=message, best_ask=plan.get("best_ask"), limit_price=plan.get("limit_price"), shares=plan.get("shares")))
                self._register_copied_deal(cache, trade, copied_usd=0.0, result_ok=False)
                continue

            ok = False
            message = ""
            used_copy_usd = copy_buy_usd
            result: dict = {}

            try:
                result = self.repeat_trade(trade, plan)
                ok = bool(result.get("ok"))
                message = str(result.get("message"))
                used_copy_usd = float(result.get("copied_usd", copy_buy_usd))
            except Exception as exc:
                message = f"error: {exc}"
                result = {"ok": False, "message": message}
                self._compact(f"[red]COPY_ORDER_FAILED[/red] {trade.title} | {exc}")
                self._log_error(
                    "COPY_ORDER_FAILED",
                    str(exc),
                    payload={
                        "source_wallet": trade.wallet,
                        "title": trade.title,
                        "outcome": trade.outcome,
                        "asset": trade.asset,
                        "copied_usd": used_copy_usd,
                    },
                    exc=exc,
                )

            status = "dry_run" if ok and self.settings.dry_run else ("posted" if ok else ("skipped_ask" if "best ask" in message else "failed"))
            self._append_ledger(
                self._ledger_row(
                    trade,
                    used_copy_usd,
                    status,
                    message=message,
                    best_ask=result.get("best_ask") if isinstance(result, dict) else None,
                    limit_price=result.get("limit_price") if isinstance(result, dict) else None,
                    shares=result.get("shares") if isinstance(result, dict) else None,
                    response=result.get("response") if isinstance(result, dict) else None,
                )
            )
            repeated.append({"wallet": trade.wallet, "asset": trade.asset, "title": trade.title, "event_slug": trade.event_slug, "condition_id": trade.condition_id, "outcome": trade.outcome, "source_size": trade.size, "source_price": trade.price, "source_amount": trade.amount, "timestamp": trade.timestamp, "source_hash": trade.tx_hash, "source_key": trade.dedupe_key, "copied_usd": used_copy_usd, "result_ok": ok, "result_message": message})
            self._register_copied_deal(cache, trade, copied_usd=used_copy_usd if ok else 0.0, result_ok=ok)

            if ok:
                seen.add(trade.dedupe_key)
                window_spend[window_key] += used_copy_usd
                cycle_spend += used_copy_usd
                day_spend += used_copy_usd
                resp_obj = result.get("response") if isinstance(result, dict) else None
                self._compact(f"[green]LIMIT BUY[/green] {trade.title} | {trade.outcome} | {result.get('shares', 0):g} @ {result.get('limit_price', 0):.4f} | ${used_copy_usd:.2f}")
                self._log_copy_deal(
                    trade,
                    copied_usd=used_copy_usd,
                    dry_run=self.settings.dry_run,
                    response=None if self.settings.dry_run else resp_obj,
                )
            elif not message.startswith("error:"):
                self._compact(f"[yellow]COPY_SKIPPED[/yellow] {trade.title} | {message}")
                self._log_error(
                    "COPY_ORDER_SKIPPED",
                    message,
                    payload={
                        "source_wallet": trade.wallet,
                        "title": trade.title,
                        "outcome": trade.outcome,
                        "asset": trade.asset,
                        "copied_usd": used_copy_usd,
                        "result_ok": ok,
                    },
                )

            self._debug("Trade result", {"title": trade.title, "outcome": trade.outcome, "asset": trade.asset, "source_amount": trade.amount, "copied_usd": used_copy_usd, "ok": ok, "message": message})

        cache["seen"] = list(seen)
        cache["repeated"] = repeated
        self._save_cache(cache)

    def loop(self, limit: int = 50, clear_cache_on_start: bool = False) -> None:
        wallets = self.load_watch_wallets()
        if clear_cache_on_start:
            self.clear_cache()
            self._debug("Cache cleared on run start")
        followed = ", ".join(wallets) if wallets else "(none)"
        self._compact(f"[cyan]FOLLOWING[/cyan] {followed}")
        self._compact(
            f"[cyan]COPY SETTINGS[/cyan] LIMIT BUY source_mult={COPY_SOURCE_USDC_MULTIPLIER:g}; "
            f"min_shares={MIN_COPY_SHARES:g}; min=${MIN_COPY_USDC:g}; max=${MAX_COPY_USDC:g}; window_cap=${MAX_COPY_USDC_PER_WINDOW:g}; "
            f"cycle_cap=${MAX_COPY_USDC_PER_CYCLE:g}; day_cap=${MAX_COPY_USDC_PER_DAY:g}; "
            f"assets={','.join(ACTIVITY_ALLOWED_ASSETS) or 'any'}; window={ACTIVITY_REQUIRED_WINDOW_MINUTES or 'any'}m; "
            f"max_ask={MAX_COPY_ASK_PRICE:g}; slippage_bps={ACTIVITY_BUY_SLIPPAGE_BPS}; auth={self.auth_mode}"
        )
        first_cycle = True
        while True:
            try:
                self.cycle(limit=limit, activity_fetch_multiplier=3 if first_cycle else 1)
                first_cycle = False
            except Exception as exc:
                self._compact(f"[red]CYCLE_ERROR[/red] {exc}")
                self._log_error("CYCLE_ERROR", str(exc), exc=exc)
            time.sleep(self.settings.poll_interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket activity-based copy trader")
    parser.add_argument("command", choices=["once", "run", "pnl"], help="Run one cycle, loop forever, or reconcile copied PnL")
    parser.add_argument("--limit", type=int, default=50, help="How many recent /activity items per wallet to fetch")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    tracker = MarketActivityTracker(settings, Console())
    if args.command == "pnl":
        tracker.write_pnl_reports(limit=max(args.limit, 500))
        return
    wallets = tracker.load_watch_wallets()
    followed = ", ".join(wallets) if wallets else "(none)"
    if args.command == "once":
        tracker._compact(f"[cyan]FOLLOWING[/cyan] {followed}")
        tracker._compact(
            f"[cyan]COPY SETTINGS[/cyan] LIMIT BUY source_mult={COPY_SOURCE_USDC_MULTIPLIER:g}; "
            f"min_shares={MIN_COPY_SHARES:g}; min=${MIN_COPY_USDC:g}; max=${MAX_COPY_USDC:g}; window_cap=${MAX_COPY_USDC_PER_WINDOW:g}; "
            f"cycle_cap=${MAX_COPY_USDC_PER_CYCLE:g}; day_cap=${MAX_COPY_USDC_PER_DAY:g}; "
            f"assets={','.join(ACTIVITY_ALLOWED_ASSETS) or 'any'}; window={ACTIVITY_REQUIRED_WINDOW_MINUTES or 'any'}m; "
            f"max_ask={MAX_COPY_ASK_PRICE:g}; slippage_bps={ACTIVITY_BUY_SLIPPAGE_BPS}; auth={tracker.auth_mode}"
        )
        tracker.cycle(limit=args.limit, activity_fetch_multiplier=3)
        return
    tracker.loop(limit=args.limit, clear_cache_on_start=CLEAR_CACHE_ON_RUN_START)


if __name__ == "__main__":
    main()
