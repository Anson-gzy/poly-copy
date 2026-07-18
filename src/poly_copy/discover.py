"""Discover copyable wallets from Polymarket leaderboard (guide filters)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any

DATA = "https://data-api.polymarket.com"
_UA = {"accept": "application/json", "user-agent": "Mozilla/5.0 (compatible; poly-copy/0.1)"}

_RETRYABLE_HTTP = {429, 500, 502, 503, 504}


def _get(
    url: str,
    timeout: float = 12.0,
    *,
    retries: int = 3,
    base_delay: float = 1.0,
    _sleep=time.sleep,
) -> Any:
    """GET with exponential backoff on 429/5xx/timeout.

    Without this, a rate-limited or transient-error response was silently
    swallowed by the caller's bare `except` and treated as "wallet has no
    data" — e.g. a failed /closed-positions call defaulted win_rate to 0.0,
    which then read as a *real* 0% win rate and hard-rejected the wallet.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code not in _RETRYABLE_HTTP or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt == retries:
                raise
        _sleep(base_delay * (2**attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"unreachable: {url}")


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


def iter_leaderboard_band(
    *,
    time_period: str,
    pnl_min: float,
    pnl_max: float,
    want: int,
    page_size: int = 50,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """
    Walk leaderboard pages until we collect `want` wallets inside PnL band.
    Leaderboard is PnL-desc, so once pnl < pnl_min we can stop.
    """
    out: list[dict[str, Any]] = []
    for page in range(max_pages):
        rows = fetch_leaderboard(time_period=time_period, limit=page_size, offset=page * page_size)
        if not rows:
            break
        stop = False
        for row in rows:
            pnl = float(row.get("pnl") or 0)
            if pnl > pnl_max:
                continue
            if pnl < pnl_min:
                stop = True
                break
            out.append(row)
            if len(out) >= want:
                return out
        if stop or len(rows) < page_size:
            break
    return out


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
    win_rate: float | None = 0.0
    closed_sample: int = 0
    pass_hard: bool = False
    reject_reason: str | None = None
    tags: list[str] = field(default_factory=list)
    behavior: dict[str, Any] = field(default_factory=dict)
    # true when one or more probe calls failed after retries (rate limit /
    # 5xx / timeout) — win_rate and possibly other fields are unreliable this
    # cycle, not a genuine reading of the wallet.
    data_unavailable: bool = False
    failed_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def behavior_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact behavior snapshot from raw data-api trade rows.

    Used as the entry baseline for drift-strike detection: top domains,
    monthly trade frequency, and median per-trade notional.
    """
    from statistics import median

    from poly_copy.features import _domain_key

    domains: dict[str, int] = {}
    notionals: list[float] = []
    epochs: list[float] = []
    for t in trades:
        d = _domain_key({"event_slug": t.get("eventSlug"), "slug": t.get("slug"), "title": t.get("title")})
        domains[d] = domains.get(d, 0) + 1
        notionals.append(float(t.get("size") or 0) * float(t.get("price") or 0))
        ts = t.get("timestamp")
        if str(ts or "").lstrip("-").isdigit():
            epochs.append(float(ts))
    top = sorted(domains, key=domains.get, reverse=True)[:3]
    monthly_freq = 0.0
    if len(epochs) >= 2:
        span_days = max((max(epochs) - min(epochs)) / 86400.0, 1.0)
        monthly_freq = len(epochs) / (span_days / 30.0)
    return {
        "top_domains": top,
        "monthly_freq": round(monthly_freq, 2),
        "median_trade_notional": round(float(median(notionals)), 2) if notionals else 0.0,
        "n_trades": len(trades),
    }


_PROBE_ERRORS = (urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError)


def _probe(address: str) -> dict[str, Any]:
    addr = address.lower()
    out: dict[str, Any] = {
        "position_value": 0.0,
        "active_markets": 0,
        "trade_count": 0,
        "traded_markets": 0,
        "win_rate": 0.0,
        "closed_sample": 0,
        "behavior": {},
        "data_unavailable": False,
        "failed_fields": [],
    }
    failed: list[str] = []
    try:
        vals = _get(f"{DATA}/value?user={addr}")
        if isinstance(vals, list) and vals:
            out["position_value"] = float(vals[0].get("value") or 0)
    except _PROBE_ERRORS:
        failed.append("value")
    try:
        positions = _get(f"{DATA}/positions?user={addr}&limit=50")
        if isinstance(positions, list):
            out["active_markets"] = len(
                {p.get("conditionId") or p.get("condition_id") for p in positions if p}
            )
            if out["position_value"] <= 0:
                out["position_value"] = sum(float(p.get("currentValue") or 0) for p in positions)
    except _PROBE_ERRORS:
        failed.append("positions")
    try:
        traded = _get(f"{DATA}/traded?user={addr}")
        if isinstance(traded, dict):
            out["traded_markets"] = int(traded.get("traded") or 0)
    except _PROBE_ERRORS:
        failed.append("traded")
    try:
        # 100 recent trades: enough for the trades>=20 floor AND for a
        # behavior baseline (domains / monthly freq / median trade size).
        trades = _get(f"{DATA}/trades?user={addr}&limit=100")
        if isinstance(trades, list):
            out["trade_count"] = len(trades)
            out["behavior"] = behavior_stats(trades)
    except _PROBE_ERRORS:
        failed.append("trades")
    closed_failed = False
    try:
        # /closed-positions defaults to realizedPnl-desc (winners first) with a
        # 50-row page cap → win_rate would read 1.0. Paginate with explicit sort.
        closed: list[Any] = []
        for offset in (0, 50, 100, 150):
            page = _get(
                f"{DATA}/closed-positions?user={addr}"
                f"&sortBy=realizedpnl&sortDirection=asc&limit=50&offset={offset}"
            )
            if not isinstance(page, list) or not page:
                break
            closed.extend(page)
            if len(page) < 50:
                break
        if closed:
            wins = sum(1 for c in closed if float(c.get("realizedPnl") or 0) > 0)
            losses = sum(1 for c in closed if float(c.get("realizedPnl") or 0) < 0)
            decided = wins + losses
            out["closed_sample"] = decided
            out["win_rate"] = (wins / decided) if decided else 0.0
    except _PROBE_ERRORS:
        closed_failed = True
        failed.append("closed_positions")

    # win_rate is only meaningful with a real decided sample. closed_sample==0
    # (whether from a genuinely empty history or a failed/rate-limited
    # request) must not be read as a real 0% win rate.
    if out["closed_sample"] == 0 or closed_failed:
        out["win_rate"] = None

    out["failed_fields"] = failed
    out["data_unavailable"] = len(failed) > 0
    return out


def _screen_reject(
    c: DiscoverCandidate,
    cfg: dict[str, Any],
    *,
    screen_key: str = "hard_screen",
) -> str | None:
    """Apply entry (`hard_screen`) or exit (`exit_screen`) thresholds.

    `exit_screen` re-validation of an *existing* member never hard-rejects on
    a probe that failed after retries (rate limit / 5xx / timeout) — the
    caller keeps the member's prior state and tags it "data_stale" instead of
    burning a strike. `hard_screen` (a brand-new candidate) does the opposite:
    missing data means we skip the wallet rather than admit it on a false 0.
    """
    if screen_key == "exit_screen" and c.data_unavailable:
        return None
    if screen_key != "exit_screen" and c.data_unavailable:
        return f"data_unavailable:{','.join(c.failed_fields)}"

    hs = cfg.get(screen_key) or cfg.get("hard_screen", {})
    if c.pnl < float(hs.get("pnl_min", 15000)):
        return f"pnl_below_min:{c.pnl:.0f}"
    pnl_max = hs.get("pnl_max", None)
    if pnl_max is not None and c.pnl > float(pnl_max):
        return f"pnl_above_max:{c.pnl:.0f}"
    if c.position_value < float(hs.get("position_value_min", 5000)):
        return f"position_value_low:{c.position_value:.0f}"
    if c.active_markets < int(hs.get("active_markets_min", 2)):
        return f"active_markets_low:{c.active_markets}"
    trades_min = int(hs.get("trades_min", 20))
    if c.trade_count < trades_min and c.traded_markets < trades_min:
        return f"trades_low:{c.trade_count}"
    # exit screen: only judge win rate when sample is decent
    sample_min = 3 if screen_key == "exit_screen" else 5
    if (
        c.closed_sample >= sample_min
        and c.win_rate is not None
        and c.win_rate < float(hs.get("win_rate_min", 0.70))
    ):
        return f"win_rate_low:{c.win_rate:.2f}"
    if screen_key != "exit_screen" and c.closed_sample < 5:
        return "win_rate_sample_low"
    return None


def _hard_reject(c: DiscoverCandidate, cfg: dict[str, Any]) -> str | None:
    """Entry / discover reject (guide hard screen)."""
    return _screen_reject(c, cfg, screen_key="hard_screen")


def _exit_reject(c: DiscoverCandidate, cfg: dict[str, Any]) -> str | None:
    """Exit reject for existing members — harder to trigger (hysteresis)."""
    return _screen_reject(c, cfg, screen_key="exit_screen")


def discover_wallets(cfg: dict[str, Any], *, exclude: set[str] | None = None) -> dict[str, Any]:
    """
    Guide flow without polymarketanalytics SaaS:
    leaderboard → PnL band → probe positions/trades/win rate → hard filters.
    """
    exclude = {a.lower() for a in (exclude or set())}
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
    # oversample band rows; probes are the bottleneck
    band_want = max(max_candidates * 3, 60)
    for period in periods:
        rows = iter_leaderboard_band(
            time_period=period,
            pnl_min=pnl_min,
            pnl_max=pnl_max,
            want=band_want,
            page_size=min(100, lb_limit),
            max_pages=max(8, lb_limit // 25),
        )
        for row in rows:
            addr = str(row.get("proxyWallet") or "").lower()
            if not addr or addr in seen or addr in exclude:
                continue
            pnl = float(row.get("pnl") or 0)
            seen[addr] = DiscoverCandidate(
                address=addr,
                user_name=row.get("userName"),
                pnl=pnl,
                vol=float(row.get("vol") or 0),
                rank=str(row.get("rank")) if row.get("rank") is not None else None,
                source_period=period,
            )
            if len(seen) >= max_candidates:
                break
        if len(seen) >= max_candidates:
            break

    candidates = list(seen.values())[:max_candidates]

    def enrich(c: DiscoverCandidate) -> DiscoverCandidate:
        stats = _probe(c.address)
        c.position_value = float(stats["position_value"])
        c.active_markets = int(stats["active_markets"])
        c.trade_count = int(stats["trade_count"])
        c.traded_markets = int(stats["traded_markets"])
        c.win_rate = float(stats["win_rate"])
        c.closed_sample = int(stats["closed_sample"])
        c.behavior = dict(stats.get("behavior") or {})
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
