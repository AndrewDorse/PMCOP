from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
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

# Fixed limit-order copy settings.
CLEAR_CACHE_ON_RUN_START = False
LIMIT_COPY_PRICE = float(os.getenv("LIMIT_COPY_PRICE", "0.51"))
LIMIT_COPY_SHARES = float(os.getenv("LIMIT_COPY_SHARES", "5"))
COPY_BALANCE_FRACTION = float(os.getenv("COPY_BALANCE_FRACTION", "0.14"))
BALANCE_REFRESH_SECONDS = int(os.getenv("BALANCE_REFRESH_SECONDS", "10"))
ACTIVITY_ALLOWED_TITLE_KEYWORDS = [
    x.strip().lower()
    for x in os.getenv("ACTIVITY_ALLOWED_TITLE_KEYWORDS", "bitcoin,btc").split(",")
    if x.strip()
]
ACTIVITY_REQUIRED_WINDOW_MINUTES = int(os.getenv("ACTIVITY_REQUIRED_WINDOW_MINUTES", "5"))

DEBUG_LOG_FILENAME = "activity_runner.log"


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
    raw_id: str = ""

    @property
    def dedupe_key(self) -> str:
        tx = (self.tx_hash or "").strip().lower()
        if tx:
            return f"tx:{tx}"
        return "|".join(
            [
                self.wallet.lower(),
                self.asset,
                self.side.upper(),
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
                "locked_windows": {},
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
                "locked_windows": {},
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
        locked = cache.get("locked_windows", {})
        cache["locked_windows"] = locked if isinstance(locked, dict) else {}
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

        locked = cache.get("locked_windows", {})
        if not isinstance(locked, dict):
            cache["locked_windows"] = {}
        else:
            keep_locked = {}
            for key, payload in locked.items():
                title = ""
                if isinstance(payload, dict):
                    title = str(payload.get("title") or "")
                if title and self._is_active_title(title):
                    keep_locked[key] = payload
            cache["locked_windows"] = keep_locked

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

    def _window_lock_key(self, title: str) -> str:
        return re.sub(r"\s+", " ", title.strip().lower())

    def _is_window_locked(self, cache: dict, title: str) -> bool:
        locked = cache.get("locked_windows", {})
        if not isinstance(locked, dict):
            cache["locked_windows"] = {}
            return False
        return self._window_lock_key(title) in locked

    def _lock_window(self, cache: dict, trade: ActivityTrade, shares: float, response: Any = None) -> None:
        locked = cache.get("locked_windows", {})
        locked_windows = dict(locked) if isinstance(locked, dict) else {}
        payload = {
            "title": trade.title,
            "outcome": trade.outcome,
            "asset": trade.asset,
            "source_wallet": trade.wallet,
            "source_key": trade.dedupe_key,
            "locked_at": int(time.time()),
            "price": LIMIT_COPY_PRICE,
            "shares": shares,
        }
        if response is not None:
            payload["response"] = self._to_serializable_response(response)
        locked_windows[self._window_lock_key(trade.title)] = payload
        cache["locked_windows"] = locked_windows

    def _is_fresh_trade(self, trade: ActivityTrade) -> bool:
        max_age = int(os.getenv("ACTIVITY_MAX_AGE_SECONDS", "3600"))
        return abs(int(time.time()) - trade.timestamp) <= max_age

    def _is_active_title(self, title: str) -> bool:
        end_dt = self._market_end_et_from_title(title)
        if end_dt is None:
            return True
        return end_dt > self._now_et()

    def _is_allowed_market_title(self, title: str) -> bool:
        title_lc = title.lower()
        if ACTIVITY_ALLOWED_TITLE_KEYWORDS and not any(keyword in title_lc for keyword in ACTIVITY_ALLOWED_TITLE_KEYWORDS):
            return False
        if ACTIVITY_REQUIRED_WINDOW_MINUTES > 0:
            window_minutes = self._window_minutes_from_title(title)
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
        return ActivityTrade(wallet=wallet, asset=asset, title=title, outcome=outcome, side=side, size=size, price=price, timestamp=timestamp, amount=size * price, tx_hash=tx_hash, raw_id=raw_id)

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

    def get_copy_buy_usd(self) -> float:
        """Return the notional implied by balance-based sizing."""
        return self._calculate_limit_order_shares() * LIMIT_COPY_PRICE

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

    def _calculate_limit_order_shares(self) -> float:
        balance = self.get_cached_available_usdc_balance()
        balance_shares = (balance * COPY_BALANCE_FRACTION) / LIMIT_COPY_PRICE if LIMIT_COPY_PRICE > 0 else 0.0
        shares = max(LIMIT_COPY_SHARES, balance_shares)
        return int(shares * 100) / 100.0

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

    def _build_limit_order(self, trade: ActivityTrade, shares: float) -> OrderArgs:
        return OrderArgs(
            token_id=trade.asset,
            price=LIMIT_COPY_PRICE,
            size=shares,
            side=Side.BUY,
        )

    def repeat_trade(self, trade: ActivityTrade, copy_buy_usd: float = 0.0) -> dict:
        opts = self._partial_market_options(trade.asset)
        shares = self._calculate_limit_order_shares()
        order_args = self._build_limit_order(trade, shares)
        copied_usd = LIMIT_COPY_PRICE * shares
        if self.settings.dry_run:
            return {
                "ok": True,
                "message": f"DRY_RUN would place limit BUY (GTC) for {trade.title} / {trade.outcome}",
                "copied_usd": copied_usd,
                "shares": shares,
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
        return {"ok": True, "message": f"placed limit BUY (GTC) for {trade.title} / {trade.outcome}", "copied_usd": copied_usd, "shares": shares, "response": resp}

    def cycle(self, limit: int = 50, activity_fetch_multiplier: int = 1) -> None:
        wallets = self.load_watch_wallets()
        if not wallets:
            raise RuntimeError("Watch list is empty. Add at least one wallet first.")

        cache = self._load_cache()
        self._prune_forbidden_buy_markets(cache)
        seen = set(str(x) for x in cache.get("seen", []))
        repeated = list(cache.get("repeated", []))
        candidates: List[ActivityTrade] = []

        skip_ended_title = os.getenv("ACTIVITY_SKIP_ENDED_TITLE_FILTER", "true").lower() in {"1", "true", "yes", "y", "on"}

        for wallet in wallets:
            trades = self.fetch_recent_wallet_activity(wallet, limit=limit, multiplier=activity_fetch_multiplier)
            skipped_seen = skipped_fresh = skipped_title = 0
            skipped_market = 0
            for trade in trades:
                if trade.dedupe_key in seen:
                    skipped_seen += 1
                    continue
                if not self._is_fresh_trade(trade):
                    skipped_fresh += 1
                    continue
                if skip_ended_title and not self._is_active_title(trade.title):
                    skipped_title += 1
                    continue
                if not self._is_allowed_market_title(trade.title):
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
                    "allowed_title_keywords": ACTIVITY_ALLOWED_TITLE_KEYWORDS,
                    "required_window_minutes": ACTIVITY_REQUIRED_WINDOW_MINUTES,
                    "skip_ended_title_filter": skip_ended_title,
                },
            )

        candidates.sort(key=lambda x: x.timestamp, reverse=True)
        latest_by_window: dict[str, ActivityTrade] = {}
        for trade in candidates:
            latest_by_window.setdefault(self._window_lock_key(trade.title), trade)
        candidates = sorted(latest_by_window.values(), key=lambda x: x.timestamp)

        if not candidates:
            self._save_cache(cache)
            return

        for trade in candidates:
            if self._is_window_locked(cache, trade.title):
                self._debug("Skip locked window", {"asset": trade.asset, "title": trade.title, "outcome": trade.outcome})
                continue

            copy_buy_usd = self.get_copy_buy_usd()
            ok = False
            message = ""
            used_copy_usd = copy_buy_usd
            used_shares = LIMIT_COPY_SHARES
            result: dict = {}

            try:
                result = self.repeat_trade(trade)
                ok = bool(result.get("ok"))
                message = str(result.get("message"))
                used_copy_usd = float(result.get("copied_usd", copy_buy_usd))
                used_shares = float(result.get("shares", used_shares))
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

            repeated.append({"wallet": trade.wallet, "asset": trade.asset, "title": trade.title, "outcome": trade.outcome, "source_size": trade.size, "source_price": trade.price, "source_amount": trade.amount, "timestamp": trade.timestamp, "source_hash": trade.tx_hash, "source_key": trade.dedupe_key, "copied_usd": used_copy_usd, "shares": used_shares, "result_ok": ok, "result_message": message})

            if ok:
                seen.add(trade.dedupe_key)
                resp_obj = result.get("response") if isinstance(result, dict) else None
                self._lock_window(cache, trade, used_shares, response=None if self.settings.dry_run else resp_obj)
                self._compact(f"[green]LIMIT ORDER[/green] {trade.title} | {trade.outcome} | {used_shares:g} @ {LIMIT_COPY_PRICE:.2f}")
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
            f"[cyan]COPY SETTINGS[/cyan] limit BUY {COPY_BALANCE_FRACTION:.0%} balance, min {LIMIT_COPY_SHARES:g} shares @ {LIMIT_COPY_PRICE:.2f}; "
            f"keywords={','.join(ACTIVITY_ALLOWED_TITLE_KEYWORDS) or 'any'}; "
            f"window={ACTIVITY_REQUIRED_WINDOW_MINUTES or 'any'}m; balance_refresh={BALANCE_REFRESH_SECONDS}s; auth={self.auth_mode}"
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
    parser.add_argument("command", choices=["once", "run"], help="Run one cycle or loop forever")
    parser.add_argument("--limit", type=int, default=50, help="How many recent /activity items per wallet to fetch")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    tracker = MarketActivityTracker(settings, Console())
    wallets = tracker.load_watch_wallets()
    followed = ", ".join(wallets) if wallets else "(none)"
    if args.command == "once":
        tracker._compact(f"[cyan]FOLLOWING[/cyan] {followed}")
        tracker._compact(
            f"[cyan]COPY SETTINGS[/cyan] limit BUY {COPY_BALANCE_FRACTION:.0%} balance, min {LIMIT_COPY_SHARES:g} shares @ {LIMIT_COPY_PRICE:.2f}; "
            f"keywords={','.join(ACTIVITY_ALLOWED_TITLE_KEYWORDS) or 'any'}; "
            f"window={ACTIVITY_REQUIRED_WINDOW_MINUTES or 'any'}m; balance_refresh={BALANCE_REFRESH_SECONDS}s; auth={tracker.auth_mode}"
        )
        tracker.cycle(limit=args.limit, activity_fetch_multiplier=3)
        return
    tracker.loop(limit=args.limit, clear_cache_on_start=CLEAR_CACHE_ON_RUN_START)


if __name__ == "__main__":
    main()
