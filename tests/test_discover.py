"""Probe retry/backoff and data-unavailable handling (rate-limit misfire fix)."""

from __future__ import annotations

import json
import urllib.error

import pytest

from poly_copy.config import load_config
from poly_copy.discover import DiscoverCandidate, _exit_reject, _get, _hard_reject, _probe


def test_get_retries_on_429_then_succeeds(monkeypatch):
    calls = {"n": 0}
    sleeps: list[float] = []

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, None)

        class Resp:
            def read(self_inner):
                return b'{"ok": true}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return Resp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = _get("https://data-api.polymarket.com/x", retries=3, base_delay=0.001, _sleep=sleeps.append)
    assert out == {"ok": True}
    assert calls["n"] == 3
    assert len(sleeps) == 2  # slept before retry 2 and 3


def test_get_raises_on_non_retryable_4xx(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        _get("https://data-api.polymarket.com/x", retries=3, base_delay=0.001, _sleep=lambda s: None)


def test_probe_marks_data_unavailable_on_persistent_failure(monkeypatch):
    """All probe calls fail after exhausting retries → win_rate=None,
    data_unavailable=True, not a silent 0.0 win rate."""

    def always_fail(url, timeout=12.0, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("poly_copy.discover._get", always_fail)
    out = _probe("0xabc")
    assert out["win_rate"] is None
    assert out["data_unavailable"] is True
    assert "closed_positions" in out["failed_fields"]


def test_probe_zero_closed_sample_is_none_not_zero(monkeypatch):
    """Wallet has genuinely no decided closed positions: win_rate is None
    (unknown), not a real 0% — but this alone is not a request failure."""

    def fake_get(url, timeout=12.0, **kwargs):
        if "closed-positions" in url:
            return []
        if "/value" in url:
            return [{"value": 6000}]
        if "/positions" in url:
            return []
        if "/traded" in url:
            return {"traded": 25}
        if "/trades" in url:
            return [{"eventSlug": "nba-x", "size": 10, "price": 0.5, "timestamp": "1700000000"}] * 25
        return []

    monkeypatch.setattr("poly_copy.discover._get", fake_get)
    out = _probe("0xabc")
    assert out["win_rate"] is None
    assert out["closed_sample"] == 0
    # no exception was raised anywhere, so nothing is flagged as a hard failure
    assert out["data_unavailable"] is False


def test_exit_reject_skips_data_unavailable_no_strike():
    cfg = load_config()
    c = DiscoverCandidate(
        address="0xabc",
        user_name=None,
        pnl=50000,
        vol=1,
        rank="1",
        source_period="MONTH",
        position_value=0.0,  # would normally fail exit_screen position_value_min
        active_markets=0,
        trade_count=0,
        traded_markets=0,
        win_rate=None,
        closed_sample=0,
        data_unavailable=True,
        failed_fields=["value", "positions", "closed_positions"],
    )
    assert _exit_reject(c, cfg) is None


def test_hard_reject_rejects_data_unavailable_new_wallet():
    """Entry screen: missing data means don't admit the wallet (宁缺毋滥)."""
    cfg = load_config()
    c = DiscoverCandidate(
        address="0xabc",
        user_name=None,
        pnl=50000,
        vol=1,
        rank="1",
        source_period="MONTH",
        position_value=10000,
        active_markets=5,
        trade_count=30,
        traded_markets=30,
        win_rate=None,
        closed_sample=0,
        data_unavailable=True,
        failed_fields=["closed_positions"],
    )
    reason = _hard_reject(c, cfg)
    assert reason is not None
    assert "data_unavailable" in reason


def test_exit_reject_still_strikes_real_low_win_rate():
    """When data IS available and the wallet genuinely has a low win rate on
    a decent sample, the exit screen still rejects (real signal must still
    strike — the fix only protects against fabricated zeros)."""
    cfg = load_config()
    c = DiscoverCandidate(
        address="0xabc",
        user_name=None,
        pnl=50000,
        vol=1,
        rank="1",
        source_period="MONTH",
        position_value=2000,
        active_markets=3,
        trade_count=25,
        traded_markets=30,
        win_rate=0.2,
        closed_sample=20,
        data_unavailable=False,
    )
    reason = _exit_reject(c, cfg)
    assert reason is not None
    assert "win_rate_low" in reason
