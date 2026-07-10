"""Discover copyable wallets from Polymarket leaderboard (guide filters)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any

DATA = "https://data-api.polymarket.com"
_UA = {"accept": "application/json", "user-agent": "Mozilla/5.0 (compatible; poly-copy/0.1)"}


def _get(url: str, timeout: float = 12.0) -> Any:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_leaderboard(
    *,
    time_period: str = "MONTH",
    order_by: str = "PNL",
    limit: int = 100,
    offset: int = 0,
    category: str = "OVERALL",
) -> list[dict[str, Any]]:
    q = urllib.parse.urlencode(
        {
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
            "offset": offset,
            "category": category,
        }
    )
    data = _get(f"{DATA}/v1/leaderboard?{q}")
    return data if isinstance(data, list) else []


@dataclass
class DiscoverCandidate:
    address: str
    user_name: str | None
    pnl: float
    vol: float
    rank: str | None
    source_period: str
    position_value: float = 0.0
    active_markets: int = 0
    trade_count: int = 0
    traded_markets: int = 0
    win_rate: float = 0.0
    closed_sample: int = 0
    pass_hard: bool = False
    reject_reason: str | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _probe(address: str) -> dict[str, Any]:
    addr = address.lower()
    out: dict[str, Any] = {
        "position_value": 0.0,
        "active_markets": 0,
        "trade_count": 0,
        "traded_markets": 0,
        "win_rate": 0.0,
        "closed_sample": 0,
    }
    try:
        vals = _get(f"{DATA}/value?user={addr}")
        if isinstance(vals, list) and vals:
            out["position_value"] = float(vals[0].get("value") or 0)
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        positions = _get(f"{DATA}/positions?user={addr}&limit=50")
        if isinstance(positions, list):
            out["active_markets"] = len(
                {p.get("conditionId") or p.get("condition_id") for p in positions if p}
            )
            if out["position_value"] <= 0:
                out["position_value"] = sum(float(p.get("currentValue") or 0) for p in positions)
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        traded = _get(f"{DATA}/traded?user={addr}")
        if isinstance(traded, dict):
            out["traded_markets"] = int(traded.get("traded") or 0)
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        # guide: trades >= 20 — fetch 25 is enough to pass/fail floor
        trades = _get(f"{DATA}/trades?user={addr}&limit=25")
        if isinstance(trades, list):
            out["trade_count"] = len(trades)
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        closed = _get(f"{DATA}/closed-positions?user={addr}&limit=50")
        if isinstance(closed, list) and closed:
            wins = sum(1 for c in closed if float(c.get("realizedPnl") or 0) > 0)
            losses = sum(1 for c in closed if float(c.get("realizedPnl") or 0) < 0)
            decided = wins + losses
            out["closed_sample"] = decided
            out["win_rate"] = (wins / decided) if decided else 0.0
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return out


def _hard_reject(c: DiscoverCandidate, cfg: dict[str, Any]) -> str | None:
    hs = cfg.get("hard_screen", {})
    if c.pnl < float(hs.get("pnl_min", 15000)):
        return f"pnl_below_min:{c.pnl:.0f}"
    if c.pnl > float(hs.get("pnl_max", 400000)):
        return f"pnl_above_max:{c.pnl:.0f}"
    if c.position_value < float(hs.get("position_value_min", 5000)):
        return f"position_value_low:{c.position_value:.0f}"
    if c.active_markets < int(hs.get("active_markets_min", 2)):
        return f"active_markets_low:{c.active_markets}"
    # trade_count from sample: if we got a full page of 25, treat as >= 25
    trades_min = int(hs.get("trades_min", 20))
    if c.trade_count < trades_min and c.traded_markets < trades_min:
        return f"trades_low:{c.trade_count}"
    if c.closed_sample >= 5 and c.win_rate < float(hs.get("win_rate_min", 0.70)):
        return f"win_rate_low:{c.win_rate:.2f}"
    if c.closed_sample < 5:
        return "win_rate_sample_low"
    return None


def discover_wallets(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Guide flow without polymarketanalytics SaaS:
    leaderboard → PnL band → probe positions/trades/win rate → hard filters.
    """
    dcfg = cfg.get("discover", {})
    periods = list(dcfg.get("time_periods", ["MONTH", "ALL"]))
    lb_limit = int(dcfg.get("leaderboard_limit", 100))
    max_candidates = int(dcfg.get("max_candidates", 40))
    max_results = int(dcfg.get("max_results", 10))
    workers = int(dcfg.get("workers", 8))

    hs = cfg.get("hard_screen", {})
    pnl_min = float(hs.get("pnl_min", 15000))
    pnl_max = float(hs.get("pnl_max", 400000))

    seen: dict[str, DiscoverCandidate] = {}
    for period in periods:
        rows = fetch_leaderboard(time_period=period, limit=lb_limit)
        for row in rows:
            addr = str(row.get("proxyWallet") or "").lower()
            if not addr or addr in seen:
                continue
            pnl = float(row.get("pnl") or 0)
            if pnl < pnl_min or pnl > pnl_max:
                continue
            seen[addr] = DiscoverCandidate(
                address=addr,
                user_name=row.get("userName"),
                pnl=pnl,
                vol=float(row.get("vol") or 0),
                rank=str(row.get("rank")) if row.get("rank") is not None else None,
                source_period=period,
            )

    candidates = list(seen.values())[:max_candidates]

    def enrich(c: DiscoverCandidate) -> DiscoverCandidate:
        stats = _probe(c.address)
        c.position_value = float(stats["position_value"])
        c.active_markets = int(stats["active_markets"])
        c.trade_count = int(stats["trade_count"])
        c.traded_markets = int(stats["traded_markets"])
        c.win_rate = float(stats["win_rate"])
        c.closed_sample = int(stats["closed_sample"])
        reason = _hard_reject(c, cfg)
        c.reject_reason = reason
        c.pass_hard = reason is None
        if c.pass_hard:
            c.tags.append("guide_pass")
        else:
            c.tags.append("guide_reject")
        return c

    enriched: list[DiscoverCandidate] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(enrich, c) for c in candidates]
        for fut in as_completed(futs):
            enriched.append(fut.result())

    passed = [c for c in enriched if c.pass_hard]
    passed.sort(key=lambda c: c.pnl, reverse=True)
    rejected = [c for c in enriched if not c.pass_hard]
    rejected.sort(key=lambda c: c.pnl, reverse=True)

    return {
        "filters": {
            "pnl": [pnl_min, pnl_max],
            "position_value_min": hs.get("position_value_min", 5000),
            "active_markets_min": hs.get("active_markets_min", 2),
            "trades_min": hs.get("trades_min", 20),
            "win_rate_min": hs.get("win_rate_min", 0.70),
            "time_periods": periods,
            "leaderboard_limit": lb_limit,
            "max_candidates": max_candidates,
        },
        "scanned": len(enriched),
        "passed": len(passed),
        "results": [c.to_dict() for c in passed[:max_results]],
        "rejected_sample": [c.to_dict() for c in rejected[:15]],
    }
