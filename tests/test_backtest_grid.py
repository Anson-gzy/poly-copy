"""poly-copy backtest --grid: param-combo replay mechanics (network mocked)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from poly_copy.backtest.grid import _filter_by_settlement, run_grid_combo
from poly_copy.config import load_config
from poly_copy.types import WalletEvent


def _ev(hours_ago: float, side: str = "BUY", price: float = 0.5, size: float = 100.0,
        wallet: str = "0xaaa", market: str = "m1", tx: str | None = None) -> WalletEvent:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return WalletEvent(
        address=wallet, side=side, size=size, price=price, notional=size * price,
        market=market, event_slug="e1", outcome="Yes", timestamp=ts,
        tx_hash=tx or f"tx{hours_ago}{side}", condition_id="c1", token_id="tok1",
    )


def test_filter_by_settlement_skips_close_to_endDate():
    now = datetime.now(timezone.utc)
    ev = _ev(0)
    ev.timestamp = now
    end_dates = {"m1": (now + timedelta(hours=1)).isoformat()}
    kept, skipped = _filter_by_settlement([ev], settlement_filter_hours=2, end_dates=end_dates)
    assert kept == []
    assert skipped == 1

    kept2, skipped2 = _filter_by_settlement([ev], settlement_filter_hours=0.5, end_dates=end_dates)
    assert len(kept2) == 1
    assert skipped2 == 0


def test_filter_by_settlement_keeps_unknown_enddate():
    ev = _ev(0)
    kept, skipped = _filter_by_settlement([ev], settlement_filter_hours=6, end_dates={})
    assert len(kept) == 1
    assert skipped == 0


def test_run_grid_combo_smoke(monkeypatch):
    cfg = load_config()
    monkeypatch.setattr("poly_copy.backtest.grid.market_is_closed", lambda **kw: False)
    monkeypatch.setattr("poly_copy.backtest.grid.market_mark_price", lambda **kw: 0.5)

    events = [_ev(5, "BUY", price=0.5), _ev(4, "SELL", price=0.6, tx="s1")]
    result = run_grid_combo(
        events,
        {"0xaaa": 1.0},
        cfg,
        fixed_notional_cap=25.0,
        stop_loss=0.7,
        settlement_filter_hours=0,
        delay_seconds=0,
        end_dates={},
        initial_capital=1000.0,
    )
    assert result.n_fills == 2
    assert result.final_equity > 0
    assert result.max_drawdown >= 0


def test_run_grid_combo_higher_delay_costs_more_slippage(monkeypatch):
    cfg = load_config()
    monkeypatch.setattr("poly_copy.backtest.grid.market_is_closed", lambda **kw: False)
    monkeypatch.setattr("poly_copy.backtest.grid.market_mark_price", lambda **kw: 0.5)

    events = [_ev(5, "BUY", price=0.5)]
    base = run_grid_combo(
        events, {"0xaaa": 1.0}, cfg, fixed_notional_cap=25.0, stop_loss=0.7,
        settlement_filter_hours=0, delay_seconds=0, end_dates={},
    )
    delayed = run_grid_combo(
        events, {"0xaaa": 1.0}, cfg, fixed_notional_cap=25.0, stop_loss=0.7,
        settlement_filter_hours=0, delay_seconds=300, end_dates={},
    )
    assert delayed.slippage_cost > base.slippage_cost
