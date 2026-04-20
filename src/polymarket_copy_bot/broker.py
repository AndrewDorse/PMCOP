from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from py_clob_client.client import ClobClient
from rich.console import Console

from .config import Settings
from .models import OrderResult, TradeInstruction


class Broker:
    def __init__(self, settings: Settings, console: Console | None = None):
        self.settings = settings
        self.console = console or Console()
        self.client = ClobClient(
            settings.pm_host,
            key=settings.pm_private_key,
            chain_id=settings.pm_chain_id,
            signature_type=settings.pm_signature_type,
            funder=settings.pm_funder,
        )

        if not settings.dry_run:
            self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def _to_serializable(self, value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, dict):
            return {k: self._to_serializable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._to_serializable(v) for v in value]
        return value

    def _extract_price(self, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        if isinstance(value, dict):
            for key in ("price", "value", "best_price", "rate"):
                if key in value:
                    parsed = self._extract_price(value[key])
                    if parsed is not None:
                        return parsed
            return None
        return None

    def _extract_level_price(self, level: Any) -> float | None:
        if level is None:
            return None
        if isinstance(level, dict):
            for key in ("price", "p", "value", "rate"):
                if key in level:
                    parsed = self._extract_price(level[key])
                    if parsed is not None:
                        return parsed
            for key in ("level", "order"):
                if key in level:
                    parsed = self._extract_level_price(level[key])
                    if parsed is not None:
                        return parsed
            for v in level.values():
                parsed = self._extract_price(v)
                if parsed is not None:
                    return parsed
            return None
        if isinstance(level, (list, tuple)):
            for item in level:
                parsed = self._extract_level_price(item)
                if parsed is not None:
                    return parsed
            return None
        return self._extract_price(level)

    def _best_book_price(self, book: Any, side: str) -> float | None:
        if not book:
            return None

        if isinstance(book, dict):
            bids = book.get("bids") or book.get("buy") or []
            asks = book.get("asks") or book.get("sell") or []
        else:
            bids = getattr(book, "bids", []) or []
            asks = getattr(book, "asks", []) or []

        levels = asks if side == "BUY" else bids
        if not levels:
            return None

        return self._extract_level_price(levels[0])

    def _market_price_for_instruction(self, inst: TradeInstruction) -> float:
        bps = self.settings.buy_slippage_bps if inst.side == "BUY" else self.settings.sell_slippage_bps
        mult = 1 + (bps / 10000.0) if inst.side == "BUY" else 1 - (bps / 10000.0)

        book_price: float | None = None
        try:
            book = self.client.get_order_book(inst.asset)
            book_price = self._best_book_price(book, inst.side)
        except Exception:
            book_price = None

        if book_price is None:
            ref = float(inst.ref_price or 0.0)
            if 0.001 < ref < 0.999:
                self.console.print(
                    f"[yellow]orderbook missing/empty for {inst.asset}; "
                    f"using Data API ref_price {ref:.4f}[/yellow]"
                )
                book_price = ref
            else:
                raise RuntimeError(f"no usable orderbook price for token {inst.asset}")

        return max(0.001, min(0.999, book_price * mult))

    def place_instruction(self, inst: TradeInstruction) -> OrderResult:
        if self.settings.dry_run:
            return OrderResult(
                ok=True,
                asset=inst.asset,
                side=inst.side,
                size=inst.size,
                price=inst.ref_price,
                message=f"DRY_RUN would place {inst.side} {inst.size:.4f} of {inst.asset} at ref {inst.ref_price:.4f}",
                raw=self._to_serializable(inst),
            )

        try:
            px = self._market_price_for_instruction(inst)
        except Exception as exc:
            return OrderResult(
                ok=False,
                asset=inst.asset,
                side=inst.side,
                size=inst.size,
                price=inst.ref_price,
                message=f"error: {exc}",
                raw=None,
            )

        try:
            order_args = {
                "token_id": inst.asset,
                "price": float(px),
                "size": float(inst.size),
                "side": inst.side.lower(),
            }
            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order)

            return OrderResult(
                ok=True,
                asset=inst.asset,
                side=inst.side,
                size=inst.size,
                price=px,
                message=f"placed {inst.side} {inst.size:.4f} of {inst.asset} at {px:.4f}",
                raw=self._to_serializable(resp),
            )
        except Exception as exc:
            return OrderResult(
                ok=False,
                asset=inst.asset,
                side=inst.side,
                size=inst.size,
                price=px,
                message=f"error: {exc}",
                raw=None,
            )
