from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Dict, List, Sequence

from rich.console import Console
from rich.table import Table

from .broker import Broker
from .config import Settings
from .data_api import PolymarketDataApi
from .models import OrderResult, Position, SyncResult, TradeInstruction
from .utils import load_json, save_json

try:
    from zoneinfo import ZoneInfo

    ET_TZ = ZoneInfo("America/New_York")
except Exception:
    # Fallback only if zoneinfo/tzdata is unavailable.
    # March markets shown in your logs are in EDT (UTC-4).
    ET_TZ = timezone(timedelta(hours=-4))


class CopyTradingEngine:
    RANGE_TITLE_RE = re.compile(
        r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*"
        r"(?P<start>\d{1,2}(?::\d{2})?\s*[AP]M)\s*-\s*"
        r"(?P<end>\d{1,2}(?::\d{2})?\s*[AP]M)\s*ET",
        re.IGNORECASE,
    )

    SINGLE_TITLE_RE = re.compile(
        r"(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2}),\s*"
        r"(?P<at>\d{1,2}(?::\d{2})?\s*[AP]M)\s*ET",
        re.IGNORECASE,
    )

    def __init__(self, settings: Settings, console: Console | None = None):
        self.settings = settings
        self.console = console or Console()
        self.data_api = PolymarketDataApi()
        self._broker: Broker | None = None
        self.settings.prepare_dirs()

    @property
    def broker(self) -> Broker:
        if self._broker is None:
            self._broker = Broker(self.settings, self.console)
        return self._broker

    def load_watch_wallets(self) -> List[str]:
        data = load_json(self.settings.watchlist_file, {"wallets": []})
        wallets = [str(w).lower() for w in data.get("wallets", []) if str(w).strip()]
        return sorted(set(wallets))

    def save_watch_wallets(self, wallets: Sequence[str]) -> None:
        save_json(self.settings.watchlist_file, {"wallets": sorted(set(w.lower() for w in wallets))})

    def add_wallet(self, wallet: str) -> None:
        wallets = self.load_watch_wallets()
        wallets.append(wallet)
        self.save_watch_wallets(wallets)

    def remove_wallet(self, wallet: str) -> None:
        wallets = [w for w in self.load_watch_wallets() if w.lower() != wallet.lower()]
        self.save_watch_wallets(wallets)

    def _now_et(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(ET_TZ)

    def _parse_time_string(self, raw: str) -> dt_time:
        cleaned = raw.replace(" ", "").upper()
        if ":" in cleaned:
            return datetime.strptime(cleaned, "%I:%M%p").time()
        return datetime.strptime(cleaned, "%I%p").time()

    def _infer_year(self, pos: Position) -> int:
        end_raw = (pos.end_date or "").strip()
        if end_raw:
            # Supports YYYY-MM-DD and full ISO datetimes
            try:
                if len(end_raw) >= 10:
                    return int(end_raw[:4])
            except Exception:
                pass
        return self._now_et().year

    def _parse_title_end_dt_et(self, pos: Position) -> datetime | None:
        title = (pos.title or "").strip()
        if not title:
            return None

        year = self._infer_year(pos)

        m = self.RANGE_TITLE_RE.search(title)
        if m:
            month = m.group("month")
            day = int(m.group("day"))
            end_time = self._parse_time_string(m.group("end"))
            base = datetime.strptime(f"{month} {day} {year}", "%B %d %Y")
            return datetime(
                year=base.year,
                month=base.month,
                day=base.day,
                hour=end_time.hour,
                minute=end_time.minute,
                second=0,
                tzinfo=ET_TZ,
            )

        m = self.SINGLE_TITLE_RE.search(title)
        if m:
            month = m.group("month")
            day = int(m.group("day"))
            at_time = self._parse_time_string(m.group("at"))
            base = datetime.strptime(f"{month} {day} {year}", "%B %d %Y")
            return datetime(
                year=base.year,
                month=base.month,
                day=base.day,
                hour=at_time.hour,
                minute=at_time.minute,
                second=0,
                tzinfo=ET_TZ,
            )

        return None

    def _parse_end_date_fallback_et(self, pos: Position) -> datetime | None:
        end_raw = (pos.end_date or "").strip()
        if not end_raw:
            return None

        # Full ISO datetime
        try:
            dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ET_TZ)
            else:
                dt = dt.astimezone(ET_TZ)
            return dt
        except Exception:
            pass

        # Date-only fallback: treat as END OF ET DAY, not start of day
        try:
            d = datetime.strptime(end_raw, "%Y-%m-%d")
            return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=ET_TZ)
        except Exception:
            return None

    def market_is_still_tradeable(self, pos: Position) -> bool:
        if not self.settings.exclude_ended_markets:
            return True

        now_et = self._now_et()

        # Prefer title-derived ET close times for intraday markets
        title_end_et = self._parse_title_end_dt_et(pos)
        if title_end_et is not None:
            return title_end_et > now_et

        # Fallback to API end_date
        fallback_end_et = self._parse_end_date_fallback_et(pos)
        if fallback_end_et is not None:
            return fallback_end_et > now_et

        # If we truly cannot determine, do not filter it out blindly
        return True

    def _filter_reason(self, pos: Position) -> str | None:
        if pos.asset in self.settings.blocked_token_ids:
            return "blocked_token"
        if pos.condition_id in self.settings.blocked_condition_ids:
            return "blocked_condition"
        prefixes = self.settings.allowed_event_slug_prefixes
        if prefixes and not any((pos.event_slug or "").startswith(p) for p in prefixes):
            return "prefix_mismatch"
        if not self.market_is_still_tradeable(pos):
            return "ended_market"
        return None

    def filters_allow(self, pos: Position) -> bool:
        return self._filter_reason(pos) is None

    def get_own_positions(self) -> Dict[str, Position]:
        raw = self.data_api.get_positions(self.settings.pm_funder)
        self.console.print(f"[cyan]Own raw positions fetched:[/cyan] {len(raw)}")

        out: Dict[str, Position] = {}
        reasons = defaultdict(int)
        sample_rejected = []

        for p in raw:
            if p.size <= 0:
                reasons["non_positive"] += 1
                continue

            reason = self._filter_reason(p)
            if reason is None:
                out[p.asset] = p
            else:
                reasons[reason] += 1
                if len(sample_rejected) < 8:
                    sample_rejected.append((p.title, p.outcome, p.end_date, reason))

        self.console.print(f"[cyan]Own usable positions:[/cyan] {len(out)}")
        self.console.print(f"[cyan]Own reject reasons:[/cyan] {dict(reasons)}")
        if sample_rejected:
            self.console.print("[cyan]Own rejected samples:[/cyan]")
            for title, outcome, end_date, reason in sample_rejected:
                self.console.print(f"  - {reason} | {title} | {outcome} | end={end_date}")
        return out

    def get_watched_positions(self, wallets: Sequence[str]) -> Dict[str, Position]:
        combined: Dict[str, Position] = {}
        per_asset_size: Dict[str, float] = defaultdict(float)
        meta: Dict[str, Position] = {}

        all_positions = self.data_api.get_positions_many(wallets)

        total_raw = 0
        total_positive = 0
        total_allowed = 0
        reasons = defaultdict(int)
        sample_rejected = []
        sample_allowed = []

        for wallet, positions in all_positions.items():
            self.console.print(f"[cyan]Wallet {wallet} raw positions:[/cyan] {len(positions)}")
            total_raw += len(positions)

            for p in positions:
                if p.size <= 0:
                    reasons["non_positive"] += 1
                    continue

                total_positive += 1
                reason = self._filter_reason(p)
                if reason is not None:
                    reasons[reason] += 1
                    if len(sample_rejected) < 10:
                        sample_rejected.append((p.title, p.outcome, p.end_date, reason))
                    continue

                total_allowed += 1
                per_asset_size[p.asset] += p.size * self.settings.copy_ratio
                meta[p.asset] = p
                if len(sample_allowed) < 10:
                    sample_allowed.append((p.title, p.outcome, p.end_date, p.size, p.cur_price))

        self.console.print(
            f"[cyan]Watched positions summary:[/cyan] raw={total_raw}, positive={total_positive}, allowed={total_allowed}, unique_assets={len(per_asset_size)}"
        )
        self.console.print(f"[cyan]Watched reject reasons:[/cyan] {dict(reasons)}")

        if sample_rejected:
            self.console.print("[cyan]Watched rejected samples:[/cyan]")
            for title, outcome, end_date, reason in sample_rejected:
                self.console.print(f"  - {reason} | {title} | {outcome} | end={end_date}")

        if sample_allowed:
            self.console.print("[cyan]Watched allowed samples:[/cyan]")
            for title, outcome, end_date, size, price in sample_allowed:
                self.console.print(f"  - {title} | {outcome} | size={size:.4f} | price={price:.4f} | end={end_date}")

        for asset, size in per_asset_size.items():
            p = meta[asset]
            combined[asset] = Position(
                wallet="aggregated",
                asset=asset,
                condition_id=p.condition_id,
                size=size,
                avg_price=p.avg_price,
                cur_price=p.cur_price,
                title=p.title,
                slug=p.slug,
                event_slug=p.event_slug,
                outcome=p.outcome,
                opposite_asset=p.opposite_asset,
                end_date=p.end_date,
                negative_risk=p.negative_risk,
            )

        return combined

    def build_sync_plan(self, wallets: Sequence[str]) -> SyncResult:
        watched_positions = self.get_watched_positions(wallets)
        own_positions = self.get_own_positions()
        skipped: List[str] = []
        instructions: List[TradeInstruction] = []

        target_sizes = {asset: pos.size for asset, pos in watched_positions.items()}
        all_assets = sorted(set(target_sizes) | set(own_positions.keys()))

        total_target_usd = sum(pos.notional_usd for pos in watched_positions.values())
        self.console.print(
            f"[cyan]Plan inputs:[/cyan] watched_assets={len(watched_positions)}, own_assets={len(own_positions)}, all_assets={len(all_assets)}, total_target_usd={total_target_usd:.4f}"
        )

        if total_target_usd > self.settings.max_total_exposure_usd:
            scale = self.settings.max_total_exposure_usd / max(total_target_usd, 1e-9)
            self.console.print(
                f"[yellow]Target exposure {total_target_usd:.2f} exceeds cap {self.settings.max_total_exposure_usd:.2f}. Scaling all watched positions by {scale:.4f}.[/yellow]"
            )
            for asset in list(target_sizes.keys()):
                target_sizes[asset] *= scale

        for asset in all_assets:
            target = float(target_sizes.get(asset, 0.0))
            own = float(own_positions.get(asset).size if asset in own_positions else 0.0)
            meta = watched_positions.get(asset) or own_positions.get(asset)
            if meta is None:
                continue

            delta = target - own
            ref_price = meta.cur_price or meta.avg_price or 0.5
            notional = abs(delta) * ref_price

            if abs(delta) < self.settings.min_delta_shares:
                skipped.append(f"{asset}: delta {delta:.4f} below MIN_DELTA_SHARES")
                continue
            if notional < self.settings.min_notional_usd:
                skipped.append(f"{asset}: notional ${notional:.4f} below MIN_NOTIONAL_USD")
                continue

            max_size_by_order = self.settings.max_single_order_usd / max(ref_price, 1e-9)
            clipped_size = min(abs(delta), max_size_by_order)
            side = "BUY" if delta > 0 else "SELL"

            instructions.append(
                TradeInstruction(
                    asset=asset,
                    condition_id=meta.condition_id,
                    side=side,
                    size=clipped_size,
                    ref_price=ref_price,
                    title=meta.title,
                    outcome=meta.outcome,
                    reason=f"sync own={own:.4f} -> target={target:.4f}",
                )
            )

        return SyncResult(
            watched_positions=watched_positions,
            own_positions=own_positions,
            target_sizes=target_sizes,
            instructions=instructions,
            skipped=skipped,
        )

    def execute_plan(self, plan: SyncResult) -> List[OrderResult]:
        results: List[OrderResult] = []
        for inst in plan.instructions:
            try:
                results.append(self.broker.place_instruction(inst))
            except Exception as exc:
                results.append(
                    OrderResult(
                        ok=False,
                        asset=inst.asset,
                        side=inst.side,
                        size=inst.size,
                        price=inst.ref_price,
                        message=f"error: {exc}",
                    )
                )
        self._append_execution_log(plan, results)
        return results

    def _append_execution_log(self, plan: SyncResult, results: List[OrderResult]) -> None:
        existing = load_json(self.settings.execution_log, [])
        existing.append(
            {
                "ts": int(time.time()),
                "instructions": [asdict(inst) for inst in plan.instructions],
                "results": [asdict(r) for r in results],
                "skipped": plan.skipped,
            }
        )
        save_json(self.settings.execution_log, existing[-500:])

    def print_plan(self, plan: SyncResult) -> None:
        table = Table(title="Copy-Trade Sync Plan")
        table.add_column("Side")
        table.add_column("Outcome")
        table.add_column("Shares")
        table.add_column("Ref Price")
        table.add_column("Reason")
        for inst in plan.instructions:
            table.add_row(
                inst.side,
                f"{inst.title} / {inst.outcome}",
                f"{inst.size:.4f}",
                f"{inst.ref_price:.4f}",
                inst.reason,
            )
        self.console.print(table)
        if plan.skipped:
            self.console.print("[dim]Skipped:[/dim]")
            for item in plan.skipped[:30]:
                self.console.print(f"  - {item}")

    def loop(self) -> None:
        wallets = self.load_watch_wallets()
        if not wallets:
            raise RuntimeError("Watch list is empty. Add at least one wallet first.")

        self.console.print(f"Watching {len(wallets)} wallet(s): {', '.join(wallets)}")
        self.console.print(f"Own trading wallet/funder: {self.settings.pm_funder}")
        self.console.print(f"DRY_RUN={self.settings.dry_run}")

        while True:
            plan = self.build_sync_plan(wallets)
            self.print_plan(plan)
            results = self.execute_plan(plan)
            for r in results:
                style = "green" if r.ok else "red"
                self.console.print(f"[{style}]{r.message}[/{style}]")
            time.sleep(self.settings.poll_interval_seconds)