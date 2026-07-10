"""Historical paper-copy replay and simple parameter scan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from poly_copy.copy import map_intent, simulate_fill
from poly_copy.risk import RiskGuard
from poly_copy.types import Allocation, PaperFill, RiskAction, WalletEvent


@dataclass
class BacktestResult:
    fills: list[PaperFill]
    equity_curve: list[float]
    total_pnl: float
    max_drawdown: float
    turnover: float
    n_stops: int
    params: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_pnl": self.total_pnl,
            "max_drawdown": self.max_drawdown,
            "turnover": self.turnover,
            "n_fills": len(self.fills),
            "n_stops": self.n_stops,
            "params": self.params,
            "final_equity": self.equity_curve[-1] if self.equity_curve else None,
        }


def _estimate_pnl(fill: PaperFill) -> float:
    """Heuristic mark-to-model PnL for paper fills without resolution data."""
    if fill.stopped:
        return fill.pnl
    # assume mean-reverting edge: buys slightly positive after cost, sells realize small edge
    edge = 0.02
    if fill.intent.side.upper() == "BUY":
        return fill.notional * (edge - fill.slippage / max(fill.fill_price, 1e-9))
    return fill.notional * (edge * 0.5)


def run_backtest(
    events: list[WalletEvent],
    allocation: Allocation,
    cfg: dict[str, Any],
    *,
    initial_capital: float | None = None,
) -> BacktestResult:
    capital = float(
        initial_capital
        if initial_capital is not None
        else cfg.get("backtest", {}).get("initial_capital", 1000.0)
    )
    guard = RiskGuard(cfg, initial_capital=capital)
    fills: list[PaperFill] = []
    equity = [capital]
    turnover = 0.0

    for ev in events:
        intent = map_intent(ev, allocation=allocation, cfg=cfg, skip_liquidity=True)
        if intent is None:
            continue
        decision = guard.check_intent(intent, fills)
        if decision.action == RiskAction.HALT and "portfolio_dd" in decision.reason:
            break
        if decision.action == RiskAction.HALT and decision.reason.startswith("wallet_dd"):
            continue
        if decision.action == RiskAction.HALT and decision.reason.startswith("stop_loss"):
            # emit synthetic stop sell
            stop_intent = CopyIntent_from(intent, side="SELL", reason="stop")
            fill = simulate_fill(stop_intent, cfg, None)
            if fill:
                fill.stopped = True
                fill.pnl = -float(cfg.get("risk", {}).get("per_trade_stop_loss", 0.7)) * fill.notional
                fills.append(fill)
                guard.on_fill(fill, fill.pnl)
                equity.append(guard.state.equity)
                turnover += fill.notional
            continue

        fill = simulate_fill(intent, cfg, decision)
        if fill is None:
            continue
        fill = guard.mark_stop_on_fill(fill)
        pnl = _estimate_pnl(fill)
        fill.pnl = pnl
        fills.append(fill)
        guard.on_fill(fill, pnl)
        equity.append(guard.state.equity)
        turnover += fill.notional

    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak)

    return BacktestResult(
        fills=fills,
        equity_curve=equity,
        total_pnl=equity[-1] - capital,
        max_drawdown=max_dd,
        turnover=turnover,
        n_stops=sum(1 for f in fills if f.stopped),
        params={
            "mode": cfg.get("copy", {}).get("mode"),
            "fixed_notional": cfg.get("copy", {}).get("fixed_notional"),
            "stop_loss": cfg.get("risk", {}).get("per_trade_stop_loss"),
        },
    )


def CopyIntent_from(intent, *, side: str, reason: str):
    from poly_copy.types import CopyIntent

    return CopyIntent(
        side=side,
        size=intent.size,
        market=intent.market,
        reason=reason,
        source_address=intent.source_address,
        price=intent.price,
        event_slug=intent.event_slug,
        outcome=intent.outcome,
        timestamp=intent.timestamp,
    )


def param_scan(
    events: list[WalletEvent],
    allocation: Allocation,
    base_cfg: dict[str, Any],
) -> list[BacktestResult]:
    import copy

    bt = base_cfg.get("backtest", {})
    notionals = list(bt.get("fixed_notionals", [25]))
    stops = list(bt.get("stop_losses", [0.7]))
    results: list[BacktestResult] = []
    for n in notionals:
        for s in stops:
            cfg = copy.deepcopy(base_cfg)
            cfg.setdefault("copy", {})["fixed_notional"] = float(n)
            cfg.setdefault("copy", {})["mode"] = "fixed"
            cfg.setdefault("risk", {})["per_trade_stop_loss"] = float(s)
            results.append(run_backtest(events, allocation, cfg))
    results.sort(key=lambda r: r.total_pnl, reverse=True)
    return results
