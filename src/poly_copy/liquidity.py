"""Market liquidity checks via Gamma public API (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GAMMA = "https://gamma-api.polymarket.com"
_CACHE: dict[str, float] = {}


def _get_json(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (compatible; poly-copy/0.1)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def market_liquidity(*, slug: str | None = None, condition_id: str | None = None) -> float | None:
    """Return market liquidity USD if found. Cached in-process."""
    key = (slug or "") + "|" + (condition_id or "")
    if not key.strip("|"):
        return None
    if key in _CACHE:
        return _CACHE[key]

    liq: float | None = None
    try:
        if slug:
            q = urllib.parse.urlencode({"slug": slug})
            data = _get_json(f"{GAMMA}/markets?{q}")
            rows = data if isinstance(data, list) else []
            if rows:
                liq = float(rows[0].get("liquidity") or rows[0].get("liquidityNum") or 0) or None
        if liq is None and condition_id:
            q = urllib.parse.urlencode({"condition_ids": condition_id})
            data = _get_json(f"{GAMMA}/markets?{q}")
            rows = data if isinstance(data, list) else []
            if rows:
                liq = float(rows[0].get("liquidity") or rows[0].get("liquidityNum") or 0) or None
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        liq = None

    if liq is not None:
        _CACHE[key] = liq
    return liq


def trade_ok(
    *,
    trade_notional: float,
    liquidity: float | None,
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    """
    Gate a single follow: need deep book, and leader trade must not dominate liquidity.
    """
    liq_cfg = cfg.get("liquidity", {})
    min_liq = float(liq_cfg.get("min_market_liquidity", 10_000))
    max_share = float(liq_cfg.get("max_trade_liquidity_share", 0.15))

    if liquidity is None:
        # unknown → fail closed for live copy speed path; research can override
        if liq_cfg.get("allow_unknown", False):
            return True, "liq_unknown_allowed"
        return False, "liq_unknown"
    if liquidity < min_liq:
        return False, f"liq_thin:{liquidity:.0f}"
    if liquidity > 0 and trade_notional / liquidity > max_share:
        return False, f"liq_dominated:{trade_notional / liquidity:.2f}"
    return True, "ok"


def enrich_trade_liquidities(
    trades: list[dict[str, Any]],
    *,
    max_markets: int = 40,
) -> dict[str, float]:
    """Map market key → liquidity for unique slugs in trades (capped)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    keys: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for t in trades:
        slug = t.get("slug") or t.get("event_slug")
        cid = t.get("condition_id")
        k = str(slug or cid or "")
        if not k or k in seen:
            continue
        seen.add(k)
        keys.append((k, slug, cid))
        if len(keys) >= max_markets:
            break

    out: dict[str, float] = {}

    def one(item: tuple[str, str | None, str | None]) -> tuple[str, float | None]:
        k, slug, cid = item
        return k, market_liquidity(slug=slug, condition_id=cid)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(one, item) for item in keys]
        for fut in as_completed(futs):
            k, liq = fut.result()
            if liq is not None:
                out[k] = liq
    return out
