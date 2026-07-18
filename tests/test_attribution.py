"""poly-copy attribution: aggregation from ledger fills / wallet_realized fallback."""

from __future__ import annotations

from poly_copy.attribution import build_attribution
from poly_copy.ledger import new_ledger, save_equity, save_ledger


def test_attribution_from_fills(tmp_path):
    led = new_ledger(1000)
    led["fills"] = [
        {"ts": 1.0, "side": "BUY", "market": "m1", "outcome": "Yes", "domain": "nba",
         "source": "0xaaa", "size": 10, "price": 0.5, "notional": 5.0, "slippage": 0.05,
         "pnl": 0.0, "reason": "copy_buy"},
        {"ts": 2.0, "side": "SELL", "market": "m1", "outcome": "Yes", "domain": "nba",
         "source": "0xaaa", "size": 10, "price": 0.4, "notional": 4.0, "slippage": 0.05,
         "pnl": -1.0, "reason": "copy_sell"},
        {"ts": 3.0, "side": "SELL", "market": "m2", "outcome": "No", "domain": "esports",
         "source": "0xbbb", "size": 10, "price": 0.6, "notional": 6.0, "slippage": 0.02,
         "pnl": 2.0, "reason": "evict_universe_churn"},
    ]
    led_path = tmp_path / "ledger.json"
    eq_path = tmp_path / "equity.json"
    save_ledger(led, led_path)
    save_equity([{"equity": 1000, "ts": "t0"}, {"equity": 1001, "ts": "t1"}], eq_path)

    report = build_attribution(ledger_path=led_path, equity_path=eq_path, fetch_end_dates=False)
    assert report["data_source"] == "wallet_realized_cumulative+fills_detail"
    assert report["n_fills_logged"] == 3
    assert report["by_source_wallet"]["0xaaa"]["realized_pnl_in_logged_fills"] == -1.0
    assert report["by_source_wallet"]["0xbbb"]["realized_pnl_in_logged_fills"] == 2.0
    assert report["by_domain"]["nba"]["realized_pnl"] == -1.0
    assert report["by_domain"]["esports"]["realized_pnl"] == 2.0
    assert report["slippage"]["total_cost"] > 0
    assert report["turnover_cost_from_universe_churn"]["realized_pnl"] == 2.0
    assert report["turnover_cost_from_universe_churn"]["n_evict_fills_logged"] == 1


def test_attribution_falls_back_to_wallet_realized_when_no_fills(tmp_path):
    led = new_ledger(1000)
    led["wallet_realized"] = {"0xaaa": -50.0, "0xbbb": 10.0}
    led_path = tmp_path / "ledger.json"
    eq_path = tmp_path / "equity.json"
    save_ledger(led, led_path)
    save_equity([{"equity": 1000, "ts": "t0"}, {"equity": 960, "ts": "t1"}], eq_path)

    report = build_attribution(ledger_path=led_path, equity_path=eq_path, fetch_end_dates=False)
    assert report["data_source"] == "wallet_realized_cumulative_only"
    assert report["n_fills_logged"] == 0
    assert report["by_source_wallet"]["0xaaa"]["realized_pnl_cumulative"] == -50.0
    assert "note" in report["by_source_wallet"]["0xaaa"]
    assert "_note" in report["by_domain"]
    assert report["total_pnl"] == -40.0
