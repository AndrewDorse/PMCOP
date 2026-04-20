from __future__ import annotations

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class Settings(BaseModel):
    pm_host: str = Field(default="https://clob.polymarket.com")
    pm_chain_id: int = Field(default=137)
    pm_private_key: str
    pm_funder: str
    pm_signature_type: int = Field(default=1)

    poll_interval_seconds: int = Field(default=8)
    copy_ratio: float = Field(default=1.0)
    min_delta_shares: float = Field(default=1.0)
    min_notional_usd: float = Field(default=1.0)
    buy_slippage_bps: int = Field(default=200)
    sell_slippage_bps: int = Field(default=200)
    max_single_order_usd: float = Field(default=100.0)
    max_total_exposure_usd: float = Field(default=1000.0)
    dry_run: bool = Field(default=True)
    exclude_ended_markets: bool = Field(default=True)

    state_dir: Path = Field(default=Path("./state"))
    watchlist_file: Path = Field(default=Path("./state/watch_wallets.json"))
    execution_log: Path = Field(default=Path("./state/execution_log.json"))

    allowed_event_slug_prefixes: List[str] = Field(default_factory=list)
    blocked_condition_ids: List[str] = Field(default_factory=list)
    blocked_token_ids: List[str] = Field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        def csv(name: str) -> List[str]:
            raw = os.getenv(name, "").strip()
            if not raw:
                return []
            return [x.strip() for x in raw.split(",") if x.strip()]

        return cls(
            pm_host=os.getenv("PM_HOST", "https://clob.polymarket.com"),
            pm_chain_id=int(os.getenv("PM_CHAIN_ID", "137")),
            pm_private_key=os.getenv("PM_PRIVATE_KEY", ""),
            pm_funder=os.getenv("PM_FUNDER", ""),
            pm_signature_type=int(os.getenv("PM_SIGNATURE_TYPE", "1")),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "8")),
            copy_ratio=float(os.getenv("COPY_RATIO", "1.0")),
            min_delta_shares=float(os.getenv("MIN_DELTA_SHARES", "1.0")),
            min_notional_usd=float(os.getenv("MIN_NOTIONAL_USD", "1.0")),
            buy_slippage_bps=int(os.getenv("BUY_SLIPPAGE_BPS", "200")),
            sell_slippage_bps=int(os.getenv("SELL_SLIPPAGE_BPS", "200")),
            max_single_order_usd=float(os.getenv("MAX_SINGLE_ORDER_USD", "100.0")),
            max_total_exposure_usd=float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "1000.0")),
            dry_run=os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes", "y"},
            exclude_ended_markets=os.getenv("EXCLUDE_ENDED_MARKETS", "true").lower() in {"1", "true", "yes", "y"},
            state_dir=Path(os.getenv("STATE_DIR", "./state")),
            watchlist_file=Path(os.getenv("WATCHLIST_FILE", "./state/watch_wallets.json")),
            execution_log=Path(os.getenv("EXECUTION_LOG", "./state/execution_log.json")),
            allowed_event_slug_prefixes=csv("ALLOWED_EVENT_SLUG_PREFIXES"),
            blocked_condition_ids=csv("BLOCKED_CONDITION_IDS"),
            blocked_token_ids=csv("BLOCKED_TOKEN_IDS"),
        )

    def prepare_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.watchlist_file.parent.mkdir(parents=True, exist_ok=True)
        self.execution_log.parent.mkdir(parents=True, exist_ok=True)
