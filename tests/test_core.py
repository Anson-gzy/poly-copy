"""Unit tests for blacklist, Fixed sizing, wallet stop-loss."""

from __future__ import annotations

from datetime import datetime, timezone

from poly_copy.config import load_config
from poly_copy.copy import map_intent, simulate_fill
from poly_copy.liquidity import trade_ok
from poly_copy.risk import RiskGuard, detect_drift
from poly_copy.score import blacklist, hard_screen, score_wallet
from poly_copy.types import CopyIntent, RiskAction, WalletEvent, WalletFeatures


def _feat(**kwargs) -> WalletFeatures:
    base = dict(
        address="0xabc",
        sample_days=90,
        trade_count=50,
        monthly_freq=40,
        win_rate=0.75,
        profit_factor=1.8,
        max_drawdown=0.1,
        focus_score=0.8,
        stability_score=0.7,
        position_value=10000,
        realized_pnl=20000,
        unrealized_pnl=1000,
        total_pnl=21000,
        active_markets=5,
        top_domains=["politics"],
        avg_position_value=500,
        single_event_pnl_share=0.2,
        meta={"open_count": 3},
    )
    base.update(kwargs)
    return WalletFeatures(**base)


def test_blacklist_hft():
    cfg = load_config()
    assert blacklist(_feat(monthly_freq=500), cfg) is not None


def test_blacklist_single_event():
    cfg = load_config()
    assert blacklist(_feat(single_event_pnl_share=0.95), cfg) is not None


def test_hard_screen_win_rate():
    cfg = load_config()
    assert hard_screen(_feat(win_rate=0.5), cfg) is not None


def test_score_suitable():
    cfg = load_config()
    sc = score_wallet(_feat(), cfg)
    assert sc.suitable
    assert sc.score > 0


def test_fixed_sizing():
    cfg = load_config()
    cfg["copy"]["mode"] = "fixed"
    cfg["copy"]["fixed_notional"] = 25.0
    ev = WalletEvent(
        address="0xabc",
        side="BUY",
        size=100,
        price=0.5,
        notional=50,
        market="m1",
        event_slug="e1",
        outcome="Yes",
        timestamp=datetime.now(timezone.utc),
    )
    intent = map_intent(ev, allocation={"0xabc": 1.0}, cfg=cfg, skip_liquidity=True)
    assert intent is not None
    assert abs(intent.size * 0.5 - 25.0) < 1e-6
    fill = simulate_fill(intent, cfg)
    assert fill is not None
    assert fill.fill_price > 0.5  # buy slippage


def test_wallet_stop_loss():
    cfg = load_config()
    cfg["risk"]["per_trade_stop_loss"] = 0.70
    guard = RiskGuard(cfg, initial_capital=1000)
    # open entry
    buy = CopyIntent(
        side="BUY",
        size=10,
        market="m1",
        reason="fixed",
        source_address="0xabc",
        price=0.80,
    )
    fill = simulate_fill(buy, cfg)
    assert fill
    guard.on_fill(fill, 0.0)
    # try to buy more after 70% adverse move (price 0.24)
    more = CopyIntent(
        side="BUY",
        size=10,
        market="m1",
        reason="fixed",
        source_address="0xabc",
        price=0.24,
    )
    decision = guard.check_intent(more)
    assert decision.action == RiskAction.HALT
    assert "stop_loss" in decision.reason


def test_drift_detection():
    cfg = load_config()
    base = _feat(monthly_freq=40, focus_score=0.9)
    cur = _feat(monthly_freq=200, focus_score=0.4)
    d = detect_drift(base, cur, cfg)
    assert d.action == RiskAction.HALT


def test_liquidity_gate_rejects_thin_and_dominated():
    cfg = load_config()
    ok, _ = trade_ok(trade_notional=100, liquidity=50_000, cfg=cfg)
    assert ok
    bad, reason = trade_ok(trade_notional=100, liquidity=500, cfg=cfg)
    assert not bad and "liq_thin" in reason
    dom, reason2 = trade_ok(trade_notional=20_000, liquidity=50_000, cfg=cfg)
    assert not dom and "liq_dominated" in reason2


def test_blacklist_thin_markets():
    cfg = load_config()
    f = _feat(
        liquid_trade_share=0.2,
        median_market_liquidity=1000,
        max_trade_liquidity_share=0.1,
        meta={"open_count": 0, "liquidity_markets_known": 10},
    )
    assert blacklist(f, cfg) is not None


def test_discover_hard_reject_pnl_band():
    from poly_copy.discover import DiscoverCandidate, _hard_reject

    cfg = load_config()
    c = DiscoverCandidate(
        address="0xabc",
        user_name=None,
        pnl=5000,
        vol=1,
        rank="1",
        source_period="MONTH",
        position_value=10000,
        active_markets=3,
        trade_count=25,
        traded_markets=30,
        win_rate=0.8,
        closed_sample=20,
    )
    assert _hard_reject(c, cfg) is not None
    c.pnl = 50000
    assert _hard_reject(c, cfg) is None
