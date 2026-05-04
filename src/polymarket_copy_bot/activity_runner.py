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
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from rich.console import Console

from .config import Settings
from .utils import load_json, save_json

try:
    from zoneinfo import ZoneInfo
    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    ET_TZ = timezone(timedelta(hours=-4))

USDC_DECIMALS = 1_000_000

# V2-style sizing: flat $1 when balance ≤ threshold; above threshold use 0.75% of balance (capped at balance).
HIGH_BALANCE_COPY_THRESHOLD_USD = 200.0
COPY_BALANCE_FRACTION_ABOVE_THRESHOLD = 0.0075  # 0.75%
MIN_MARKET_COPY_USD = 1.0
CLEAR_CACHE_ON_RUN_START = True

# Same outcome token (contract): allow repeat buys after this cooldown (seconds).
ASSET_BUY_COOLDOWN_SECONDS = 15 * 60

DEBUG_LOG_FILENAME = "activity_runner.log"


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
                "repeated": [],
                "forbidden_buy_markets": {},
                "market_outcome_entries": {},
                "asset_last_buy_ts": {},
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

    def _is_fresh_trade(self, trade: ActivityTrade) -> bool:
        max_age = int(os.getenv("ACTIVITY_MAX_AGE_SECONDS", "3600"))
        return abs(int(time.time()) - trade.timestamp) <= max_age

    def _is_active_title(self, title: str) -> bool:
        end_dt = self._market_end_et_from_title(title)
        if end_dt is None:
            return True
        return end_dt > self._now_et()

    def _can_buy_asset_after_cooldown(self, cache: dict, asset: str) -> tuple[bool, str]:
        raw = cache.get("asset_last_buy_ts", {})
        if not isinstance(raw, dict):
            cache["asset_last_buy_ts"] = {}
            return True, ""
        last_ts = int(raw.get(asset, 0) or 0)
        now_ts = int(time.time())
        if last_ts > 0 and now_ts - last_ts < ASSET_BUY_COOLDOWN_SECONDS:
            return False, "asset_buy_cooldown"
        return True, ""

    def _register_asset_buy_time(self, cache: dict, asset: str) -> None:
        raw = cache.get("asset_last_buy_ts", {})
        times = dict(raw) if isinstance(raw, dict) else {}
        times[asset] = int(time.time())
        cache["asset_last_buy_ts"] = times

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
        """$1 flat when balance ≤ $200; above $200 use 0.75% of balance. Capped at balance (V2 min $1)."""
        balance = self.get_available_usdc_balance()
        if balance < MIN_MARKET_COPY_USD:
            self._debug("Insufficient USDC for $1 copy", {"balance": balance})
            return 0.0
        if balance <= HIGH_BALANCE_COPY_THRESHOLD_USD:
            amount = MIN_MARKET_COPY_USD
        else:
            amount = balance * COPY_BALANCE_FRACTION_ABOVE_THRESHOLD
        amount = min(amount, balance)
        return int(amount * 100) / 100.0

    def _build_market_order(self, trade: ActivityTrade, copy_buy_usd: float) -> MarketOrderArgs:
        return MarketOrderArgs(token_id=trade.asset, amount=float(copy_buy_usd), side=BUY, order_type=OrderType.FOK)

    def repeat_trade(self, trade: ActivityTrade, copy_buy_usd: float) -> dict:
        if copy_buy_usd <= 0:
            return {"ok": False, "message": "skipped: need at least $1 USDC for copy"}
        if self.settings.dry_run:
            order_args = self._build_market_order(trade, copy_buy_usd)
            return {"ok": True, "message": f"DRY_RUN would place market BUY for {trade.title} / {trade.outcome}", "copied_usd": copy_buy_usd, "order_args": {"token_id": order_args.token_id, "amount": order_args.amount, "side": order_args.side, "order_type": str(order_args.order_type)}}
        order_args = self._build_market_order(trade, copy_buy_usd)
        signed = self.client.create_market_order(order_args)
        resp = self.client.post_order(signed, OrderType.FOK)
        return {"ok": True, "message": f"placed market BUY for {trade.title} / {trade.outcome}", "copied_usd": copy_buy_usd, "response": resp}

    def cycle(self, limit: int = 50) -> None:
        wallets = self.load_watch_wallets()
        if not wallets:
            raise RuntimeError("Watch list is empty. Add at least one wallet first.")

        cache = self._load_cache()
        self._prune_forbidden_buy_markets(cache)
        seen = set(str(x) for x in cache.get("seen", []))
        repeated = list(cache.get("repeated", []))
        cycle_bought_assets: set[str] = set()
        candidates: List[ActivityTrade] = []

        for wallet in wallets:
            trades = self.fetch_recent_wallet_activity(wallet, limit=limit)
            self._debug("Fetched activity", {"wallet": wallet, "count": len(trades)})
            for trade in trades:
                if trade.dedupe_key in seen:
                    continue
                if not self._is_fresh_trade(trade):
                    continue
                if not self._is_active_title(trade.title):
                    continue
                candidates.append(trade)

        candidates.sort(key=lambda x: x.timestamp)

        if not candidates:
            self._save_cache(cache)
            return

        for trade in candidates:
            if trade.asset in cycle_bought_assets:
                self._debug("Skip same cycle asset", {"asset": trade.asset, "title": trade.title})
                continue

            allowed_cd, cd_reason = self._can_buy_asset_after_cooldown(cache, trade.asset)
            if not allowed_cd:
                self._debug("Skip asset cooldown", {"asset": trade.asset, "title": trade.title, "reason": cd_reason})
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
                cycle_bought_assets.add(trade.asset)
                self._register_asset_buy_time(cache, trade.asset)
                self._compact(f"[green]OPEN DEAL[/green] {trade.title} | {trade.outcome} | ${used_copy_usd:.2f}")

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
