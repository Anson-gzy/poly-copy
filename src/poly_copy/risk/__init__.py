"""Stop-loss, behavior drift, portfolio circuit breakers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from poly_copy.types import (
    CopyIntent,
    PaperFill,
    RiskAction,
    RiskDecision,
    WalletFeatures,
)


@dataclass
class RiskState:
    """Mutable risk state across a paper session."""

    equity: float = 0.0
    peak_equity: float = 0.0
    wallet_pnl: dict[str, float] = field(default_factory=dict)
    wallet_peak: dict[str, float] = field(default_factory=dict)
    halted: bool = False
    halt_reason: str = ""
    open_entries: dict[str, float] = field(default_factory=dict)  # market -> entry price


class RiskGuard:
    def __init__(self, cfg: dict[str, Any], initial_capital: float = 1000.0):
        self.cfg = cfg
        self.risk = cfg.get("risk", {})
        self.state = RiskState(equity=initial_capital, peak_equity=initial_capital)

    def on_fill(self, fill: PaperFill, mark_pnl: float = 0.0) -> None:
        addr = fill.intent.source_address
        self.state.wallet_pnl[addr] = self.state.wallet_pnl.get(addr, 0.0) + mark_pnl
        self.state.equity += mark_pnl
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)
        peak = self.state.wallet_peak.get(addr, 0.0)
        cur = self.state.wallet_pnl[addr]
        self.state.wallet_peak[addr] = max(peak, cur)

        key = fill.intent.market
        if fill.intent.side.upper() == "BUY":
            self.state.open_entries[key] = fill.fill_price
        elif fill.intent.side.upper() == "SELL":
            self.state.open_entries.pop(key, None)

    def check_intent(self, intent: CopyIntent, fills: list[PaperFill] | None = None) -> RiskDecision:
        if self.state.halted:
            return RiskDecision(RiskAction.HALT, self.state.halt_reason or "halted")

        # portfolio drawdown circuit breaker
        port_dd = 0.0
        if self.state.peak_equity > 0:
            port_dd = (self.state.peak_equity - self.state.equity) / self.state.peak_equity
        max_port_dd = float(self.risk.get("portfolio_max_drawdown", 0.25))
        if port_dd >= max_port_dd:
            self.state.halted = True
            self.state.halt_reason = f"portfolio_dd:{port_dd:.2f}"
            return RiskDecision(RiskAction.HALT, self.state.halt_reason)

        # per-wallet drawdown
        addr = intent.source_address
        w_pnl = self.state.wallet_pnl.get(addr, 0.0)
        w_peak = self.state.wallet_peak.get(addr, 0.0)
        max_w_dd = float(self.risk.get("wallet_max_drawdown", 0.18))
        if w_peak > 0:
            w_dd = (w_peak - w_pnl) / w_peak
            if w_dd >= max_w_dd:
                return RiskDecision(RiskAction.HALT, f"wallet_dd:{addr[:10]}:{w_dd:.2f}")

        # per-trade stop: if marking an open position that already lost stop_loss
        stop = float(self.risk.get("per_trade_stop_loss", 0.70))
        entry = self.state.open_entries.get(intent.market)
        if entry and intent.price is not None and intent.side.upper() == "SELL":
            # selling after adverse move is fine; buying more into loser → reduce/halt
            pass
        if entry and intent.price is not None and intent.side.upper() == "BUY":
            # adverse if price moved against long (price down from entry for YES-like)
            loss = (entry - float(intent.price)) / entry if entry else 0.0
            if loss >= stop:
                return RiskDecision(RiskAction.HALT, f"stop_loss:{loss:.2f}")

        return RiskDecision(RiskAction.ALLOW, "ok")

    def mark_stop_on_fill(self, fill: PaperFill) -> PaperFill:
        """If fill is a forced stop, annotate."""
        stop = float(self.risk.get("per_trade_stop_loss", 0.70))
        entry = self.state.open_entries.get(fill.intent.market)
        if entry and fill.intent.side.upper() == "SELL":
            loss = (entry - fill.fill_price) / entry if entry else 0.0
            if loss >= stop * 0.9:
                fill.stopped = True
                fill.note = (fill.note + " stop").strip()
                fill.pnl = -stop * fill.notional
        return fill


def drift_strikes(
    baseline: dict[str, Any],
    current: dict[str, Any],
    cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Behavior-drift strike reasons vs. the entry baseline snapshot.

    Each returned reason counts as 1 strike (universe kicks at 2 total):
    - domain drift: Jaccard(top_domains) < 0.4
    - monthly frequency jump: >2x or <0.5x baseline
    - median trade size jump: >3x baseline
    """
    drift = (cfg or {}).get("risk", {}).get("drift", {})
    jaccard_min = float(drift.get("domain_jaccard_min", 0.4))
    freq_hi = float(drift.get("freq_jump_max", 2.0))
    freq_lo = float(drift.get("freq_drop_min", 0.5))
    size_hi = float(drift.get("median_size_jump_max", 3.0))

    reasons: list[str] = []
    base_dom = {str(d).lower() for d in (baseline.get("top_domains") or []) if d}
    cur_dom = {str(d).lower() for d in (current.get("top_domains") or []) if d}
    if base_dom and cur_dom:
        jac = len(base_dom & cur_dom) / len(base_dom | cur_dom)
        if jac < jaccard_min:
            reasons.append(f"drift_domains:jaccard={jac:.2f}")

    base_freq = float(baseline.get("monthly_freq") or 0)
    cur_freq = float(current.get("monthly_freq") or 0)
    if base_freq > 0 and cur_freq > 0:
        ratio = cur_freq / base_freq
        if ratio > freq_hi or ratio < freq_lo:
            reasons.append(f"drift_freq:{ratio:.2f}x")

    base_med = float(baseline.get("median_trade_notional") or 0)
    cur_med = float(current.get("median_trade_notional") or 0)
    if base_med > 0 and cur_med > 0 and cur_med / base_med > size_hi:
        reasons.append(f"drift_size:{cur_med / base_med:.2f}x")

    return reasons


def detect_drift(
    baseline: WalletFeatures,
    current: WalletFeatures,
    cfg: dict[str, Any],
) -> RiskDecision:
    drift = cfg.get("risk", {}).get("drift", {})
    freq_max = float(drift.get("freq_ratio_max", 3.0))
    focus_drop = float(drift.get("focus_drop_min", 0.35))

    if baseline.monthly_freq > 0:
        ratio = current.monthly_freq / baseline.monthly_freq
        if ratio >= freq_max:
            return RiskDecision(RiskAction.HALT, f"drift_freq:{ratio:.1f}x")

    if baseline.focus_score - current.focus_score >= focus_drop:
        return RiskDecision(
            RiskAction.HALT,
            f"drift_focus:{baseline.focus_score:.2f}->{current.focus_score:.2f}",
        )

    return RiskDecision(RiskAction.ALLOW, "no_drift")
