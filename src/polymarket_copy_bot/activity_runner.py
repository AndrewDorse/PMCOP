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
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from rich.console import Console

from .config import Settings
from .utils import load_json, save_json

try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone(timedelta(hours=-4))

USDC_DECIMALS = 1_000_000

# Market FOK copy size: max($3, 2% of USDC balance), capped at balance.
COPY_BALANCE_FRACTION = 0.02
MIN_MARKET_COPY_USD = 3.0
IGNORE_IF_ALREADY_IN_POSITION = True
CLEAR_CACHE_ON_RUN_START = True

TP_PRICE = 0.99
TP_SIZE_FACTOR = 0.98
TP_CHECK_INTERVAL_SECONDS = 60
DEBUG_LOG_FILENAME = "activity_runner.log"

# Strict 5m/15m “Up or Down” window filter (subset of crypto activity)
ALLOWED_CRYPTO_TOKENS = ("btc", "bitcoin", "eth", "ethereum", "ether", "sol", "solana", "xtp", "tap", "xrp", "doge", "bnb", "hype")

# Same market + same outcome rules
MAX_SAME_DIRECTION_ENTRIES_PER_MARKET = 2
MIN_SECONDS_BETWEEN_SAME_DIRECTION_ENTRIES = 15


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


@dataclass
class PositionItem:
    asset: str
    size: float
    title: str
    outcome: str


@dataclass
class ClosedPositionItem:
    asset: str
    title: str
    outcome: str
    realized_pnl: float
    timestamp: int
    avg_price: float


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
        queries = [
            {"user": wallet, "limit": limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            {"wallet": wallet, "limit": limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            {"address": wallet, "limit": limit, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        ]
        items: List[dict] = []
        for q in queries:
            try:
                data = self._get("/activity", q)
            except Exception:
                continue
            if isinstance(data, list):
                items.extend(x for x in data if isinstance(x, dict))
            elif isinstance(data, dict):
                for key in ("activity", "data", "items", "results"):
                    value = data.get(key)
                    if isinstance(value, list):
                        items.extend(x for x in value if isinstance(x, dict))
        return items

    def get_positions(self, wallet: str) -> List[dict]:
        queries = [{"user": wallet}, {"address": wallet}]
        items: List[dict] = []
        for q in queries:
            try:
                data = self._get("/positions", q)
            except Exception:
                continue
            if isinstance(data, list):
                items.extend(x for x in data if isinstance(x, dict))
                if items:
                    break
            elif isinstance(data, dict):
                for key in ("positions", "data", "items", "results"):
                    value = data.get(key)
                    if isinstance(value, list):
                        items.extend(x for x in value if isinstance(x, dict))
                if items:
                    break
        return items

    def get_closed_positions(self, wallet: str, limit: int = 100) -> List[dict]:
        queries = [{"user": wallet, "limit": limit}, {"address": wallet, "limit": limit}]
        items: List[dict] = []
        for q in queries:
            try:
                data = self._get("/closed-positions", q)
            except Exception:
                continue
            if isinstance(data, list):
                items.extend(x for x in data if isinstance(x, dict))
                if items:
                    break
            elif isinstance(data, dict):
                for key in ("positions", "data", "items", "results"):
                    value = data.get(key)
                    if isinstance(value, list):
                        items.extend(x for x in value if isinstance(x, dict))
                if items:
                    break
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
            key=settings.pm_private_key,
            chain_id=settings.pm_chain_id,
            signature_type=settings.pm_signature_type,
            funder=settings.pm_funder,
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        self._last_tp_check_ts = 0

        self.logger = logging.getLogger(f"activity_runner_{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        fh = logging.FileHandler(self.log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(fh)

    def clear_cache(self) -> None:
        save_json(
            self.cache_file,
            {
                "seen": [],
                "seen_closed": [],
                "repeated": [],
                "tp_log": [],
                "forbidden_buy_markets": {},
                "market_outcome_entries": {},
            },
        )

    def _load_cache(self) -> dict:
        return load_json(
            self.cache_file,
            {
                "seen": [],
                "seen_closed": [],
                "repeated": [],
                "tp_log": [],
                "forbidden_buy_markets": {},
                "market_outcome_entries": {},
            },
        )

    def _save_cache(self, cache: dict) -> None:
        cache["seen"] = list(cache.get("seen", []))[-10000:]
        cache["seen_closed"] = list(cache.get("seen_closed", []))[-5000:]
        cache["repeated"] = list(cache.get("repeated", []))[-3000:]
        cache["tp_log"] = list(cache.get("tp_log", []))[-3000:]
        forbidden = cache.get("forbidden_buy_markets", {})
        cache["forbidden_buy_markets"] = forbidden if isinstance(forbidden, dict) else {}
        entries = cache.get("market_outcome_entries", {})
        cache["market_outcome_entries"] = entries if isinstance(entries, dict) else {}
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

    def _debug(self, message: str, payload: Optional[dict] = None) -> None:
        if payload is None:
            self.logger.info(message)
        else:
            self.logger.info("%s | %s", message, json.dumps(payload, ensure_ascii=False))

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
        for candidate in (raw.get("side"), raw.get("tradeType"), raw.get("activityType"), raw.get("type"), raw.get("action"), raw.get("verb")):
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

    def _is_crypto_window_title(self, title: str) -> bool:
        lower = title.lower()
        if "up or down" not in lower and "higher or lower" not in lower and "above or below" not in lower:
            return False
        if not any(token in lower for token in ALLOWED_CRYPTO_TOKENS):
            return False
        window = self._window_minutes_from_title(title)
        return window in (5, 15)

    def _is_fresh_trade(self, trade: ActivityTrade) -> bool:
        max_age = int(os.getenv("ACTIVITY_MAX_AGE_SECONDS", "3600"))
        return abs(int(time.time()) - trade.timestamp) <= max_age

    def _is_active_title(self, title: str) -> bool:
        end_dt = self._market_end_et_from_title(title)
        if end_dt is None:
            return True
        return end_dt > self._now_et()

    def _is_crypto_activity_title(self, title: str) -> bool:
        """Copy BUYs on strict window markets OR any market title mentioning major crypto (not only 5m/15m up/down)."""
        if self._is_crypto_window_title(title):
            return True
        low = title.lower()
        keywords = (
            "bitcoin",
            "btc",
            "ethereum",
            "solana",
            "dogecoin",
            "doge",
            "xrp",
            "ripple",
            "bnb",
            "binance",
            "hype",
            "cardano",
            "polygon",
            "matic",
        )
        return any(k in low for k in keywords)

    def _can_open_same_outcome(self, cache: dict, title: str, outcome: str) -> tuple[bool, str]:
        entries = cache.get("market_outcome_entries", {})
        if not isinstance(entries, dict):
            entries = {}
            cache["market_outcome_entries"] = entries
        key = self._market_buy_key(title, outcome)
        payload = entries.get(key, {})
        count = int(payload.get("count", 0)) if isinstance(payload, dict) else 0
        last_ts = int(payload.get("last_ts", 0)) if isinstance(payload, dict) else 0
        now_ts = int(time.time())
        if count >= MAX_SAME_DIRECTION_ENTRIES_PER_MARKET:
            return False, "max_same_direction_entries_reached"
        if last_ts > 0 and now_ts - last_ts < MIN_SECONDS_BETWEEN_SAME_DIRECTION_ENTRIES:
            return False, "same_direction_cooldown"
        return True, ""

    def _register_same_outcome_open(self, cache: dict, title: str, outcome: str, asset: str) -> None:
        entries = cache.get("market_outcome_entries", {})
        if not isinstance(entries, dict):
            entries = {}
        key = self._market_buy_key(title, outcome)
        payload = entries.get(key, {})
        prev_count = int(payload.get("count", 0)) if isinstance(payload, dict) else 0
        entries[key] = {
            "title": title,
            "outcome": outcome,
            "asset": asset,
            "count": prev_count + 1,
            "last_ts": int(time.time()),
        }
        cache["market_outcome_entries"] = entries

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
        if not title or not asset or size <= 0 or price <= 0 or timestamp <= 0:
            return None
        return ActivityTrade(wallet=wallet, asset=asset, title=title, outcome=outcome, side=side, size=size, price=price, timestamp=timestamp, amount=size * price, tx_hash=tx_hash, raw_id=raw_id)

    def _extract_position(self, raw: dict) -> Optional[PositionItem]:
        asset = str(raw.get("asset") or raw.get("tokenID") or raw.get("tokenId") or raw.get("token_id") or "").strip()
        size = self._to_float(raw.get("size") or raw.get("shares"))
        title = str(raw.get("title") or raw.get("market") or raw.get("question") or "").strip()
        outcome = str(raw.get("outcome") or raw.get("outcomeName") or "").strip()
        if not asset or size <= 0 or not title:
            return None
        return PositionItem(asset=asset, size=size, title=title, outcome=outcome)

    def _extract_closed_position(self, raw: dict) -> Optional[ClosedPositionItem]:
        asset = str(raw.get("asset") or raw.get("tokenID") or raw.get("tokenId") or raw.get("token_id") or "").strip()
        title = str(raw.get("title") or raw.get("market") or raw.get("question") or "").strip()
        outcome = str(raw.get("outcome") or raw.get("outcomeName") or "").strip()
        realized_pnl = self._to_float(raw.get("realizedPnl") or raw.get("realized_pnl"))
        avg_price = self._to_float(raw.get("avgPrice") or raw.get("avg_price"))
        timestamp = self._to_int(raw.get("timestamp") or raw.get("time") or raw.get("createdAt") or raw.get("created_at"))
        if not asset or not title or timestamp <= 0:
            return None
        return ClosedPositionItem(asset=asset, title=title, outcome=outcome, realized_pnl=realized_pnl, timestamp=timestamp, avg_price=avg_price)

    def fetch_recent_wallet_activity(self, wallet: str, limit: int = 50) -> List[ActivityTrade]:
        raw_activity = self.api.get_recent_activity(wallet, limit=limit)
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
        return items[: max(limit * 3, limit)]

    def fetch_own_positions(self) -> List[PositionItem]:
        raw_positions = self.api.get_positions(self.settings.pm_funder)
        out: List[PositionItem] = []
        for item in raw_positions:
            if not isinstance(item, dict):
                continue
            pos = self._extract_position(item)
            if pos is not None:
                out.append(pos)
        return out

    def fetch_closed_positions(self) -> List[ClosedPositionItem]:
        raw_closed = self.api.get_closed_positions(self.settings.pm_funder, limit=100)
        out: List[ClosedPositionItem] = []
        for item in raw_closed:
            if not isinstance(item, dict):
                continue
            pos = self._extract_closed_position(item)
            if pos is not None:
                out.append(pos)
        out.sort(key=lambda x: x.timestamp)
        return out

    def _has_position(self, asset: str) -> bool:
        for pos in self.fetch_own_positions():
            if pos.asset == asset and pos.size > 0:
                return True
        return False

    def _extract_usdc_balance(self, payload: Any) -> float:
        if isinstance(payload, dict):
            raw_balance = payload.get("balance")
            if raw_balance is None and "data" in payload and isinstance(payload["data"], dict):
                raw_balance = payload["data"].get("balance")
            if raw_balance is None:
                return 0.0
            try:
                return float(raw_balance) / USDC_DECIMALS
            except Exception:
                try:
                    return float(str(raw_balance))
                except Exception:
                    return 0.0
        return 0.0

    def get_available_usdc_balance(self) -> float:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=self.settings.pm_signature_type)
        payload = self.client.get_balance_allowance(params=params)
        return max(0.0, self._extract_usdc_balance(payload))

    def get_copy_buy_usd(self) -> float:
        """max($3, 2% * balance), never more than balance."""
        balance = self.get_available_usdc_balance()
        if balance < MIN_MARKET_COPY_USD:
            self._debug("Insufficient USDC for $3 copy", {"balance": balance})
            return 0.0
        amount = max(MIN_MARKET_COPY_USD, balance * COPY_BALANCE_FRACTION)
        amount = min(amount, balance)
        return int(amount * 100) / 100.0

    def _build_market_order(self, trade: ActivityTrade, copy_buy_usd: float) -> MarketOrderArgs:
        return MarketOrderArgs(token_id=trade.asset, amount=float(copy_buy_usd), side=BUY, order_type=OrderType.FOK)

    def repeat_trade(self, trade: ActivityTrade, copy_buy_usd: float) -> dict:
        if copy_buy_usd <= 0:
            return {"ok": False, "message": "skipped: need at least $3 USDC for copy"}
        if self.settings.dry_run:
            order_args = self._build_market_order(trade, copy_buy_usd)
            return {"ok": True, "message": f"DRY_RUN would place market BUY for {trade.title} / {trade.outcome}", "copied_usd": copy_buy_usd, "order_args": {"token_id": order_args.token_id, "amount": order_args.amount, "side": order_args.side, "order_type": str(order_args.order_type)}}
        order_args = self._build_market_order(trade, copy_buy_usd)
        signed = self.client.create_market_order(order_args)
        resp = self.client.post_order(signed, OrderType.FOK)
        return {"ok": True, "message": f"placed market BUY for {trade.title} / {trade.outcome}", "copied_usd": copy_buy_usd, "response": resp}

    def _get_open_orders(self) -> List[dict]:
        for method_name in ("get_open_orders", "getOpenOrders"):
            try:
                method = getattr(self.client, method_name, None)
                if callable(method):
                    orders = method()
                    return orders if isinstance(orders, list) else list(orders or [])
            except Exception:
                pass
        return []

    def _has_existing_tp_order(self, asset: str) -> bool:
        for order in self._get_open_orders():
            if not isinstance(order, dict):
                continue
            order_asset = str(order.get("asset_id") or order.get("asset") or order.get("token_id") or order.get("tokenID") or "").strip()
            order_side = str(order.get("side") or "").upper().strip()
            price = self._to_float(order.get("price"))
            if order_asset == asset and order_side == "SELL" and abs(price - TP_PRICE) < 1e-9:
                return True
        return False

    def _emit_actual_closed_deals(self, cache: dict) -> None:
        seen_closed = set(str(x) for x in cache.get("seen_closed", []))
        closed_positions = self.fetch_closed_positions()
        for pos in closed_positions:
            key = f"{pos.asset}|{pos.timestamp}|{pos.realized_pnl:.8f}|{pos.title}"
            if key in seen_closed:
                continue
            self._compact(f"[blue]CLOSE DEAL[/blue] {pos.title} | {pos.outcome} | PNL {pos.realized_pnl:+.2f}")
            self._debug("Actual close detected from closed-positions", {"asset": pos.asset, "title": pos.title, "outcome": pos.outcome, "realized_pnl": pos.realized_pnl, "avg_price": pos.avg_price, "timestamp": pos.timestamp})
            seen_closed.add(key)
        cache["seen_closed"] = list(seen_closed)

    def place_tp_orders(self, cache: Optional[dict] = None) -> None:
        now_ts = int(time.time())
        if now_ts - self._last_tp_check_ts < TP_CHECK_INTERVAL_SECONDS:
            return
        self._last_tp_check_ts = now_ts

        positions = self.fetch_own_positions()
        active_positions = [p for p in positions if self._is_crypto_activity_title(p.title) and self._is_active_title(p.title)]

        if cache is None:
            cache = self._load_cache()

        self._emit_actual_closed_deals(cache)
        tp_log = list(cache.get("tp_log", []))

        for pos in active_positions:
            tp_size = round(pos.size * TP_SIZE_FACTOR, 4)
            if tp_size <= 0:
                continue
            if self._has_existing_tp_order(pos.asset):
                self._debug("TP exists", {"asset": pos.asset, "title": pos.title, "outcome": pos.outcome})
                continue

            order_args = OrderArgs(token_id=pos.asset, price=TP_PRICE, size=tp_size, side=SELL)

            if self.settings.dry_run:
                self._debug("DRY_RUN TP", {"asset": pos.asset, "title": pos.title, "outcome": pos.outcome, "size": tp_size, "price": TP_PRICE})
                tp_log.append({"ts": now_ts, "asset": pos.asset, "title": pos.title, "outcome": pos.outcome, "size": tp_size, "price": TP_PRICE, "dry_run": True})
                continue

            try:
                signed = self.client.create_order(order_args)
                resp = self.client.post_order(signed, OrderType.GTC)
                self._debug("TP placed", {"asset": pos.asset, "title": pos.title, "outcome": pos.outcome, "size": tp_size, "price": TP_PRICE, "response": str(resp)})
                tp_log.append({"ts": now_ts, "asset": pos.asset, "title": pos.title, "outcome": pos.outcome, "size": tp_size, "price": TP_PRICE, "dry_run": False})
            except Exception as exc:
                self._debug("TP error", {"asset": pos.asset, "title": pos.title, "outcome": pos.outcome, "size": tp_size, "price": TP_PRICE, "error": str(exc)})
                tp_log.append({"ts": now_ts, "asset": pos.asset, "title": pos.title, "outcome": pos.outcome, "size": tp_size, "price": TP_PRICE, "dry_run": False, "error": str(exc)})

        cache["tp_log"] = tp_log
        self._save_cache(cache)

    def cycle(self, limit: int = 50) -> None:
        wallets = self.load_watch_wallets()
        if not wallets:
            raise RuntimeError("Watch list is empty. Add at least one wallet first.")

        cache = self._load_cache()
        self._prune_forbidden_buy_markets(cache)
        seen = set(str(x) for x in cache.get("seen", []))
        repeated = list(cache.get("repeated", []))
        cycle_bought_markets: set[str] = set()
        candidates: List[ActivityTrade] = []

        for wallet in wallets:
            trades = self.fetch_recent_wallet_activity(wallet, limit=limit)
            self._debug("Fetched activity", {"wallet": wallet, "count": len(trades)})
            for trade in trades:
                if trade.dedupe_key in seen:
                    continue
                if not self._is_crypto_activity_title(trade.title):
                    continue
                if not self._is_fresh_trade(trade):
                    continue
                if not self._is_active_title(trade.title):
                    continue
                candidates.append(trade)

        candidates.sort(key=lambda x: x.timestamp)

        if not candidates:
            self.place_tp_orders(cache=cache)
            return

        for trade in candidates:
            market_key = self._market_buy_key(trade.title, trade.outcome)
            if market_key in cycle_bought_markets:
                self._debug("Skip same cycle", {"title": trade.title, "outcome": trade.outcome})
                continue

            allowed_same, same_reason = self._can_open_same_outcome(cache, trade.title, trade.outcome)
            if not allowed_same:
                self._debug("Skip same outcome rule", {"title": trade.title, "outcome": trade.outcome, "reason": same_reason})
                continue

            if IGNORE_IF_ALREADY_IN_POSITION and self._has_position(trade.asset):
                self._debug("Skip already in position", {"title": trade.title, "outcome": trade.outcome, "asset": trade.asset})
                continue

            copy_buy_usd = self.get_copy_buy_usd()
            ok = False
            message = ""
            used_copy_usd = copy_buy_usd

            try:
                result = self.repeat_trade(trade, copy_buy_usd)
                ok = bool(result.get("ok"))
                message = str(result.get("message"))
                used_copy_usd = float(result.get("copied_usd", copy_buy_usd))
            except Exception as exc:
                message = f"error: {exc}"

            repeated.append({"wallet": trade.wallet, "asset": trade.asset, "title": trade.title, "outcome": trade.outcome, "source_size": trade.size, "source_price": trade.price, "source_amount": trade.amount, "timestamp": trade.timestamp, "source_hash": trade.tx_hash, "source_key": trade.dedupe_key, "copied_usd": used_copy_usd, "result_ok": ok, "result_message": message})

            if ok:
                seen.add(trade.dedupe_key)
                cycle_bought_markets.add(market_key)
                self._register_same_outcome_open(cache, trade.title, trade.outcome, trade.asset)
                self._compact(f"[green]OPEN DEAL[/green] {trade.title} | {trade.outcome} | ${used_copy_usd:.2f}")

            self._debug("Trade result", {"title": trade.title, "outcome": trade.outcome, "asset": trade.asset, "source_amount": trade.amount, "copied_usd": used_copy_usd, "ok": ok, "message": message})

        cache["seen"] = list(seen)
        cache["repeated"] = repeated
        self._save_cache(cache)
        self.place_tp_orders(cache=cache)

    def loop(self, limit: int = 50, clear_cache_on_start: bool = False) -> None:
        wallets = self.load_watch_wallets()
        if clear_cache_on_start:
            self.clear_cache()
            self._debug("Cache cleared on run start")
        followed = ", ".join(wallets) if wallets else "(none)"
        self._compact(f"[cyan]FOLLOWING[/cyan] {followed}")
        while True:
            try:
                self.cycle(limit=limit)
            except Exception as exc:
                self._compact(f"[red]ERROR[/red] {exc}")
                self._debug("Cycle error", {"error": str(exc)})
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
    tracker._compact(f"[cyan]FOLLOWING[/cyan] {followed}")
    if args.command == "once":
        tracker.cycle(limit=args.limit)
        return
    tracker.loop(limit=args.limit, clear_cache_on_start=CLEAR_CACHE_ON_RUN_START)


if __name__ == "__main__":
    main()
