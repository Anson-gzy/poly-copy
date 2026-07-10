"""Paper copy executor: Fixed / Portfolio sizing with slippage."""

from __future__ import annotations

from typing import Any

from poly_copy.types import Allocation, CopyIntent, PaperFill, RiskAction, RiskDecision, WalletEvent


def map_intent(
    event: WalletEvent,
    *,
    allocation: Allocation,
    cfg: dict[str, Any],
) -> CopyIntent | None:
    copy_cfg = cfg.get("copy", {})
    side = event.side.upper()
    if side == "BUY" and not copy_cfg.get("follow_buys", True):
        return None
    if side == "SELL" and not copy_cfg.get("follow_sells", True):
        return None

    mode = str(copy_cfg.get("mode", "fixed")).lower()
    addr = event.address.lower()
    alloc_norm = {a.lower(): w for a, w in allocation.items()}
    weight = float(alloc_norm.get(addr, 0.0))
    if alloc_norm and addr not in alloc_norm:
        return None

    if mode == "portfolio":
        if weight <= 0:
            return None
        capital = float(copy_cfg.get("portfolio_capital", 1000.0))
        notional = capital * weight
        reason = f"portfolio_w={weight:.3f}"
    else:
        notional = float(copy_cfg.get("fixed_notional", 25.0))
        reason = "fixed"

    price = event.price if event.price > 0 else 0.5
    size = notional / price
    return CopyIntent(
        side=side,
        size=size,
        market=event.market,
        reason=reason,
        source_address=event.address,
        price=price,
        event_slug=event.event_slug,
        outcome=event.outcome,
        timestamp=event.timestamp,
    )


def apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    slip = slippage_bps / 10000.0
    if side.upper() == "BUY":
        return price * (1.0 + slip)
    return price * (1.0 - slip)


def simulate_fill(
    intent: CopyIntent,
    cfg: dict[str, Any],
    decision: RiskDecision | None = None,
) -> PaperFill | None:
    if decision is not None:
        if decision.action == RiskAction.HALT:
            return None
        size = intent.size * (decision.scale if decision.action == RiskAction.REDUCE else 1.0)
    else:
        size = intent.size

    if size <= 0:
        return None

    slip_bps = float(cfg.get("copy", {}).get("slippage_bps", 500))
    raw_price = float(intent.price or 0.5)
    fill_price = apply_slippage(raw_price, intent.side, slip_bps)
    notional = size * fill_price
    return PaperFill(
        intent=intent,
        fill_price=fill_price,
        fill_size=size,
        notional=notional,
        slippage=abs(fill_price - raw_price),
        note=decision.reason if decision else "",
    )


def paper_copy_events(
    events: list[WalletEvent],
    allocation: Allocation,
    cfg: dict[str, Any],
    *,
    risk_fn=None,
) -> list[PaperFill]:
    """Replay events into paper fills. risk_fn(intent, fills_so_far) -> RiskDecision."""
    fills: list[PaperFill] = []
    for ev in events:
        intent = map_intent(ev, allocation=allocation, cfg=cfg)
        if intent is None:
            continue
        decision = risk_fn(intent, fills) if risk_fn else None
        fill = simulate_fill(intent, cfg, decision)
        if fill is not None:
            fills.append(fill)
    return fills
