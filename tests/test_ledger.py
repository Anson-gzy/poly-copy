"""Ledger increments, halt, eviction, sizing, quarantine, domain caps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from poly_copy.config import load_config
from poly_copy.ledger import (
    append_equity_point,
    copy_notional,
    current_equity,
    evict_drawdown_wallets,
    ingest_events,
    load_ledger,
    mark_positions,
    new_ledger,
    save_ledger,
    settle_resolved,
    update_equity_and_halt,
)
from poly_copy.portfolio import cap_domain_weights
from poly_copy.risk import drift_strikes
from poly_copy.types import WalletEvent
from poly_copy.universe import UniverseMember, apply_quarantine


def _ev(ts: float, side: str = "BUY", price: float = 0.5, size: float = 1000.0,
        wallet: str = "0xaaa", market: str = "m1", tx: str | None = None,
        token: str | None = "tok1") -> WalletEvent:
    return WalletEvent(
        address=wallet,
        side=side,
        size=size,
        price=price,
        notional=size * price,
        market=market,
        event_slug="e1",
        outcome="Yes",
        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc),
        tx_hash=tx or f"tx{ts}{side}",
        condition_id="c1",
        token_id=token,
    )


def test_copy_notional_formula():
    # leader trades 5% of a 100k portfolio; weight 0.1 of 1000 equity
    n = copy_notional(weight=0.1, equity=1000, leader_notional=5000,
                      leader_portfolio_value=100_000, per_trade_cap=50)
    assert abs(n - 0.1 * 1000 * 0.05) < 1e-9
    # unknown leader portfolio → cap dominates
    n2 = copy_notional(weight=0.1, equity=1000, leader_notional=5000,
                       leader_portfolio_value=None, per_trade_cap=50)
    assert n2 == 50.0
    assert copy_notional(weight=0.0, equity=1000, leader_notional=1,
                         leader_portfolio_value=None) == 0.0


def test_ingest_incremental_cursor():
    cfg = load_config()
    led = new_ledger(1000)
    events = [_ev(100.0), _ev(200.0, tx="tx2")]
    s1 = ingest_events(led, events, alloc={"0xaaa": 1.0}, cfg=cfg)
    assert s1["n_new"] == 2
    assert s1["n_buys"] == 2
    # replay identical events → all stale, nothing changes
    cash_before = led["cash"]
    s2 = ingest_events(led, events, alloc={"0xaaa": 1.0}, cfg=cfg)
    assert s2["n_new"] == 0
    assert s2["n_skipped_stale"] == 2
    assert led["cash"] == cash_before
    # one newer event → only it is processed
    s3 = ingest_events(led, events + [_ev(300.0, tx="tx3")], alloc={"0xaaa": 1.0}, cfg=cfg)
    assert s3["n_new"] == 1


def test_ingest_buy_then_sell_realizes_pnl():
    cfg = load_config()
    cfg["copy"]["slippage_bps"] = 0
    led = new_ledger(1000)
    ingest_events(led, [_ev(1.0, "BUY", price=0.5)], alloc={"0xaaa": 1.0}, cfg=cfg)
    assert len(led["positions"]) == 1
    pos = next(iter(led["positions"].values()))
    assert abs(pos["avg_price"] - 0.5) < 1e-9
    ingest_events(led, [_ev(2.0, "SELL", price=0.8, tx="s1")], alloc={"0xaaa": 1.0}, cfg=cfg)
    assert led["realized_pnl_cum"] > 0
    assert led["wallet_realized"]["0xaaa"] > 0


def test_wallet_exposure_cap():
    cfg = load_config()
    cfg["risk"]["wallet_exposure_cap"] = 0.10
    cfg["risk"]["per_trade_cap"] = 1000.0
    cfg["copy"]["slippage_bps"] = 0
    led = new_ledger(1000)
    # repeated big buys: total exposure from one wallet stays <= 10% equity
    events = [_ev(float(i), "BUY", price=0.5, tx=f"t{i}", token=f"tok{i}") for i in range(1, 8)]
    ingest_events(led, events, alloc={"0xaaa": 1.0}, cfg=cfg)
    exposure = sum(p["size"] * p["avg_price"] for p in led["positions"].values())
    assert exposure <= 0.10 * 1000 + 1e-6


def test_drawdown_flags_would_halt_but_keeps_copying_in_paper_mode():
    """Paper stage default (risk.halt_enabled unset/false): portfolio drawdown
    is record-only — would_halt/would_halt_reason are set, but halted stays
    False so buys keep flowing."""
    cfg = load_config()
    cfg["copy"]["slippage_bps"] = 0
    led = new_ledger(1000)
    ingest_events(led, [_ev(1.0, "BUY", price=0.5)], alloc={"0xaaa": 1.0}, cfg=cfg)
    # equity collapses >15% below high water
    led["high_water"] = 2000.0
    update_equity_and_halt(led, positions_value=0.0, halt_drawdown=0.15)
    assert not led["halted"]
    assert led["would_halt"]
    assert "portfolio_dd" in led["would_halt_reason"]
    s = ingest_events(
        led,
        [_ev(2.0, "BUY", tx="b2", token="tok9"), _ev(3.0, "SELL", price=0.6, tx="s2")],
        alloc={"0xaaa": 1.0},
        cfg=cfg,
    )
    assert s["n_skipped_halted"] == 0  # BUY not blocked in paper mode
    assert s["n_buys"] == 1
    assert s["n_sells"] == 1


def test_halt_enabled_still_blocks_buys_for_live_mode():
    """risk.halt_enabled: true (future live-trading switch) restores the old
    hard-stop behavior: halted=True blocks further BUYs, SELLs still pass."""
    cfg = load_config()
    cfg["copy"]["slippage_bps"] = 0
    led = new_ledger(1000)
    ingest_events(led, [_ev(1.0, "BUY", price=0.5)], alloc={"0xaaa": 1.0}, cfg=cfg)
    led["high_water"] = 2000.0
    update_equity_and_halt(led, positions_value=0.0, halt_drawdown=0.15, halt_enabled=True)
    assert led["halted"]
    assert led["would_halt"]
    s = ingest_events(
        led,
        [_ev(2.0, "BUY", tx="b2", token="tok9"), _ev(3.0, "SELL", price=0.6, tx="s2")],
        alloc={"0xaaa": 1.0},
        cfg=cfg,
    )
    assert s["n_skipped_halted"] == 1  # the BUY
    assert s["n_sells"] == 1  # SELL still processed


def test_settle_resolved_realizes_at_settlement():
    cfg = load_config()
    cfg["copy"]["slippage_bps"] = 0
    led = new_ledger(1000)
    ingest_events(led, [_ev(1.0, "BUY", price=0.5)], alloc={"0xaaa": 1.0}, cfg=cfg)
    settled = settle_resolved(
        led,
        is_closed_fn=lambda **kw: True,
        price_fn=lambda **kw: 1.0,  # resolved YES
    )
    assert len(settled) == 1
    assert not led["positions"]
    assert led["realized_pnl_cum"] > 0
    assert abs(current_equity(led) - led["cash"]) < 1e-9


def test_mark_positions_stale_keeps_last_value():
    cfg = load_config()
    cfg["copy"]["slippage_bps"] = 0
    led = new_ledger(1000)
    ingest_events(led, [_ev(1.0, "BUY", price=0.5)], alloc={"0xaaa": 1.0}, cfg=cfg)
    pv1 = mark_positions(led, price_fn=lambda **kw: 0.7)
    pos = next(iter(led["positions"].values()))
    assert not pos["mark_stale"]
    pv2 = mark_positions(led, price_fn=lambda **kw: None)  # price unavailable
    assert next(iter(led["positions"].values()))["mark_stale"]
    assert abs(pv1 - pv2) < 1e-9  # keeps last mark


def test_evict_wallet_drawdown_closes_positions():
    cfg = load_config()
    cfg["copy"]["slippage_bps"] = 0
    led = new_ledger(1000)
    ingest_events(led, [_ev(1.0, "BUY", price=0.5)], alloc={"0xaaa": 1.0}, cfg=cfg)
    # simulate: wallet had a big peak then gave it all back
    led["wallet_realized"]["0xaaa"] = -150.0
    led["wallet_peak"]["0xaaa"] = 100.0  # dd = 250/1000 = 25% of capital
    out = evict_drawdown_wallets(led, max_dd=0.20, price_fn=lambda **kw: 0.5)
    assert len(out) == 1
    assert out[0]["wallet"] == "0xaaa"
    assert "wallet_dd" in out[0]["reason"]
    assert not led["positions"]  # positions closed
    # under the threshold → no eviction
    led2 = new_ledger(1000)
    led2["wallet_realized"]["0xbbb"] = -50.0
    led2["wallet_peak"]["0xbbb"] = 100.0  # dd = 15%
    assert evict_drawdown_wallets(led2, max_dd=0.20, price_fn=lambda **kw: None) == []


def test_ledger_save_load_roundtrip(tmp_path):
    led = new_ledger(1000)
    led["cash"] = 900.0
    led["cursors"]["0xaaa"] = {"ts": 123.0, "tx": ["a"]}
    p = tmp_path / "ledger.json"
    save_ledger(led, p)
    loaded = load_ledger(p)
    assert loaded["cash"] == 900.0
    assert loaded["cursors"]["0xaaa"]["ts"] == 123.0
    # cold start
    fresh = load_ledger(tmp_path / "missing.json")
    assert fresh["cash"] == 1000.0
    assert not fresh["halted"]


def test_equity_points_truncation():
    pts: list = []
    for i in range(10):
        append_equity_point(
            pts, ts=f"t{i}", equity=1000 + i, cash=500, positions_value=500,
            n_open=1, realized_pnl_cum=0, max_points=5,
        )
    assert len(pts) == 5
    assert pts[-1]["equity"] == 1009


def test_drift_strikes_rules():
    cfg = load_config()
    base = {"top_domains": ["nba", "nfl", "soccer"], "monthly_freq": 40,
            "median_trade_notional": 100}
    # no drift
    assert drift_strikes(base, dict(base), cfg) == []
    # domain flip (jaccard 0), freq 3x, size 5x → 3 strikes
    cur = {"top_domains": ["crypto", "fed"], "monthly_freq": 120,
           "median_trade_notional": 500}
    reasons = drift_strikes(base, cur, cfg)
    assert len(reasons) == 3
    assert any("drift_domains" in r for r in reasons)
    assert any("drift_freq" in r for r in reasons)
    assert any("drift_size" in r for r in reasons)
    # freq collapse below 0.5x also strikes
    slow = {"top_domains": ["nba", "nfl", "soccer"], "monthly_freq": 10,
            "median_trade_notional": 100}
    assert drift_strikes(base, slow, cfg) == [r for r in drift_strikes(base, slow, cfg)]
    assert any("drift_freq" in r for r in drift_strikes(base, slow, cfg))


def test_cap_domain_weights():
    alloc = {"a": 0.35, "b": 0.35, "c": 0.15, "d": 0.15}
    domains = {"a": "nba", "b": "nba", "c": "fed", "d": "crypto"}
    out = cap_domain_weights(alloc, domains, cap=0.40)
    assert abs(sum(out.values()) - 1.0) < 1e-9
    nba = out["a"] + out["b"]
    assert nba <= 0.40 + 1e-6
    # surplus went to the other wallets
    assert out["c"] > 0.15 and out["d"] > 0.15
    # single-domain degenerate case: returned normalized unchanged
    same = cap_domain_weights({"a": 0.5, "b": 0.5}, {"a": "x", "b": "x"}, cap=0.4)
    assert abs(sum(same.values()) - 1.0) < 1e-9


def test_quarantine_halves_new_member_weight():
    now = datetime.now(timezone.utc)
    fresh = UniverseMember(address="a", score=1.0, suitable=True,
                           added_at=(now - timedelta(days=1)).isoformat())
    old = UniverseMember(address="b", score=1.0, suitable=True,
                         added_at=(now - timedelta(days=30)).isoformat())
    alloc = apply_quarantine([fresh, old], {"a": 0.5, "b": 0.5}, now=now, days=7)
    assert abs(sum(alloc.values()) - 1.0) < 1e-9
    assert alloc["a"] < alloc["b"]
    assert abs(alloc["a"] / alloc["b"] - 0.5) < 1e-9
    assert "quarantine" in fresh.tags
    assert "quarantine" not in old.tags
    # after the window the tag drops off
    later = now + timedelta(days=8)
    alloc2 = apply_quarantine([fresh, old], {"a": 0.5, "b": 0.5}, now=later, days=7)
    assert abs(alloc2["a"] - alloc2["b"]) < 1e-9
    assert "quarantine" not in fresh.tags
