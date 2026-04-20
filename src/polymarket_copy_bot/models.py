from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(slots=True)
class Position:
    wallet: str
    asset: str
    condition_id: str
    size: float
    avg_price: float
    cur_price: float
    title: str = ""
    slug: str = ""
    event_slug: str = ""
    outcome: str = ""
    opposite_asset: str = ""
    end_date: str = ""
    negative_risk: bool = False

    @property
    def notional_usd(self) -> float:
        return self.size * max(self.cur_price or self.avg_price, 0.0)


@dataclass(slots=True)
class TradeInstruction:
    asset: str
    condition_id: str
    side: str  # BUY or SELL
    size: float
    ref_price: float
    title: str = ""
    outcome: str = ""
    reason: str = ""

    @property
    def notional_usd(self) -> float:
        return self.size * self.ref_price


@dataclass(slots=True)
class WatchWallets:
    wallets: List[str] = field(default_factory=list)


@dataclass(slots=True)
class SyncResult:
    watched_positions: Dict[str, Position]
    own_positions: Dict[str, Position]
    target_sizes: Dict[str, float]
    instructions: List[TradeInstruction]
    skipped: List[str]


@dataclass(slots=True)
class OrderResult:
    ok: bool
    asset: str
    side: str
    size: float
    price: float
    message: str
    raw: Optional[dict] = None
