"""Shared contracts for the poly-copy pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


@dataclass
class WalletEvent:
    """One trade / activity row for a wallet."""

    address: str
    side: str  # BUY | SELL
    size: float
    price: float
    notional: float
    market: str
    event_slug: str
    outcome: str
    timestamp: datetime | None
    tx_hash: str | None = None
    condition_id: str | None = None
    token_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.timestamp is not None:
            d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass
class WalletFeatures:
    """Feature vector + metadata for a wallet."""

    address: str
    sample_days: float
    trade_count: int
    monthly_freq: float
    win_rate: float
    profit_factor: float
    max_drawdown: float
    focus_score: float  # 1 = concentrated in few event domains
    stability_score: float
    position_value: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    active_markets: int
    top_domains: list[str] = field(default_factory=list)
    avg_position_value: float = 0.0
    single_event_pnl_share: float = 0.0
    position_volatility: float = 0.0
    # share of trades in markets meeting min liquidity; median book depth
    liquid_trade_share: float = 0.0
    median_market_liquidity: float = 0.0
    max_trade_liquidity_share: float = 0.0  # worst trade_notional/liquidity
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalletScore:
    address: str
    score: float
    hard_reject_reason: str | None = None
    tags: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)

    @property
    def suitable(self) -> bool:
        return self.hard_reject_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "score": self.score,
            "suitable": self.suitable,
            "hard_reject_reason": self.hard_reject_reason,
            "tags": self.tags,
            "components": self.components,
        }


# address -> weight
Allocation = dict[str, float]


@dataclass
class CopyIntent:
    side: str
    size: float
    market: str
    reason: str
    source_address: str
    price: float | None = None
    event_slug: str = ""
    outcome: str = ""
    timestamp: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.timestamp is not None:
            d["timestamp"] = self.timestamp.isoformat()
        return d


class RiskAction(str, Enum):
    ALLOW = "allow"
    REDUCE = "reduce"
    HALT = "halt"


@dataclass
class RiskDecision:
    action: RiskAction
    reason: str
    scale: float = 1.0  # multiply size when REDUCE

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action.value, "reason": self.reason, "scale": self.scale}


@dataclass
class PaperFill:
    intent: CopyIntent
    fill_price: float
    fill_size: float
    notional: float
    slippage: float
    pnl: float = 0.0
    stopped: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_dict(),
            "fill_price": self.fill_price,
            "fill_size": self.fill_size,
            "notional": self.notional,
            "slippage": self.slippage,
            "pnl": self.pnl,
            "stopped": self.stopped,
            "note": self.note,
        }
