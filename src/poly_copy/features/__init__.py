"""Wallet feature engineering from guide metrics."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

import numpy as np

from poly_copy.data import WalletSnapshot
from poly_copy.types import WalletFeatures


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _domain_key(row: dict[str, Any]) -> str:
    slug = (row.get("event_slug") or row.get("slug") or row.get("title") or "unknown").lower()
    # coarse domain: first token of event slug / title keyword buckets
    for key in ("election", "trump", "biden", "crypto", "btc", "eth", "nba", "nfl", "soccer", "fed", "rate"):
        if key in slug:
            return key
    parts = slug.replace("-", " ").split()
    return parts[0] if parts else "unknown"


def compute_features(snap: WalletSnapshot) -> WalletFeatures:
    trades = snap.trades
    closed = snap.closed_positions
    positions = snap.positions

    timestamps = [_parse_ts(t.get("timestamp")) for t in trades]
    timestamps = [t for t in timestamps if t is not None]
    if timestamps:
        tmin, tmax = min(timestamps), max(timestamps)
        sample_days = max((tmax - tmin).total_seconds() / 86400.0, 1.0)
    else:
        sample_days = 1.0

    trade_count = len(trades)
    monthly_freq = trade_count / (sample_days / 30.0) if sample_days > 0 else float(trade_count)

    # win rate from closed positions (realized_pnl > 0)
    wins = sum(1 for c in closed if float(c.get("realized_pnl") or 0) > 0)
    losses = sum(1 for c in closed if float(c.get("realized_pnl") or 0) < 0)
    decided = wins + losses
    win_rate = (wins / decided) if decided else 0.0

    gross_profit = sum(float(c.get("realized_pnl") or 0) for c in closed if float(c.get("realized_pnl") or 0) > 0)
    gross_loss = abs(
        sum(float(c.get("realized_pnl") or 0) for c in closed if float(c.get("realized_pnl") or 0) < 0)
    )
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (2.0 if gross_profit > 0 else 0.0)

    realized = sum(float(c.get("realized_pnl") or 0) for c in closed)
    unrealized = sum(float(p.get("cash_pnl") or 0) for p in positions)
    if snap.leaderboard_pnl is not None:
        total_pnl = float(snap.leaderboard_pnl)
    else:
        total_pnl = realized + unrealized

    position_value = snap.portfolio_value or sum(float(p.get("current_value") or 0) for p in positions)
    avg_pos = (position_value / len(positions)) if positions else 0.0

    # equity curve proxy: cumulative closed pnl ordered by timestamp
    closed_sorted = sorted(
        closed,
        key=lambda c: _parse_ts(c.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    curve: list[float] = []
    for c in closed_sorted:
        equity += float(c.get("realized_pnl") or 0)
        curve.append(equity)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
        elif peak < 0 and equity < peak:
            max_dd = max(max_dd, abs(equity - peak) / (abs(peak) + 1e-9))

    # focus: share of trades in top 3 domains
    domains = [_domain_key(t) for t in trades] or [_domain_key(p) for p in positions]
    counts = Counter(domains)
    top = [d for d, _ in counts.most_common(3)]
    top_share = sum(counts[d] for d in top) / len(domains) if domains else 0.0
    # guide prefers 1–3 domains: high top_share + few domains is good
    n_dom = len(counts)
    focus_score = top_share * (1.0 if n_dom <= 3 else max(0.0, 1.0 - 0.1 * (n_dom - 3)))

    # stability: prefer 60–90 days sample + moderate freq + smooth equity
    longevity = min(sample_days / 60.0, 1.0)
    freq_ok = 1.0 if 30 <= monthly_freq <= 200 else max(0.0, 1.0 - abs(monthly_freq - 115) / 300)
    if len(curve) >= 2:
        rets = np.diff(curve)
        vol = float(np.std(rets)) if len(rets) else 0.0
        smooth = 1.0 / (1.0 + vol / (abs(np.mean(curve)) + 1.0))
    else:
        smooth = 0.3
    stability_score = float(0.4 * longevity + 0.3 * freq_ok + 0.3 * smooth)

    # single-event concentration of |realized pnl|
    abs_pnls = [abs(float(c.get("realized_pnl") or 0)) for c in closed]
    total_abs = sum(abs_pnls)
    single_share = (max(abs_pnls) / total_abs) if total_abs > 0 else 0.0

    pos_vals = [float(p.get("current_value") or 0) for p in positions]
    pos_vol = float(np.std(pos_vals) / (np.mean(pos_vals) + 1e-9)) if len(pos_vals) >= 2 else 0.0

    active_markets = snap.traded_market_count or len(
        {p.get("condition_id") for p in positions if p.get("condition_id")}
    )

    return WalletFeatures(
        address=snap.address,
        sample_days=float(sample_days),
        trade_count=trade_count,
        monthly_freq=float(monthly_freq),
        win_rate=float(win_rate),
        profit_factor=float(profit_factor),
        max_drawdown=float(max_dd),
        focus_score=float(focus_score),
        stability_score=float(stability_score),
        position_value=float(position_value),
        realized_pnl=float(realized),
        unrealized_pnl=float(unrealized),
        total_pnl=float(total_pnl),
        active_markets=int(active_markets),
        top_domains=top,
        avg_position_value=float(avg_pos),
        single_event_pnl_share=float(single_share),
        position_volatility=float(pos_vol),
        meta={"n_domains": n_dom, "closed_count": len(closed), "open_count": len(positions)},
    )
