"""Market liquidity checks via Gamma public API (stdlib only)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GAMMA = "https://gamma-api.polymarket.com"
_CACHE: dict[str, float] = {}
_ROW_CACHE: dict[str, dict[str, Any] | None] = {}

# Simple in-process backoff: after a failed/rate-limited call, wait this long
# before the next Gamma request. Keeps us polite without an external limiter.
_MIN_INTERVAL_S = 0.12
_last_call = 0.0


def _get_json(url: str, timeout: float = 8.0, retries: int = 2) -> Any:
    global _last_call
    req = urllib.request.Request(
        url,
        headers={
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (compatible; poly-copy/0.1)",
        },
    )
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                _last_call = time.monotonic()
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            _last_call = time.monotonic()
            last_exc = e
            if e.code == 429 or e.code >= 500:
                time.sleep(0.25 * (2**attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            _last_call = time.monotonic()
            last_exc = e
            time.sleep(0.2 * (2**attempt))
            continue
    if last_exc:
        raise last_exc
    return None


def _market_row(*, slug: str | None = None, condition_id: str | None = None) -> dict[str, Any] | None:
    """Fetch (and cache) the raw Gamma market row for a slug/condition_id.

    Gamma's `/markets?slug=` list endpoint filters out resolved/closed
    markets by default, so a wallet's historical (already-resolved) trades
    would silently miss every lookup — this was the root cause of
    liquid_trade_share / median_market_liquidity always coming back 0.
    We use the singular `/markets/slug/{slug}` endpoint (returns the market
    regardless of closed state) for slug lookups, and `closed=true` on the
    condition_id list lookup for the same reason.
    """
    key = (slug or "") + "|" + (condition_id or "")
    if not key.strip("|"):
        return None
    if key in _ROW_CACHE:
        return _ROW_CACHE[key]

    row: dict[str, Any] | None = None
    try:
        if slug:
            data = _get_json(f"{GAMMA}/markets/slug/{urllib.parse.quote(slug)}")
            if isinstance(data, dict) and data:
                row = data
        if row is None and condition_id:
            q = urllib.parse.urlencode({"condition_ids": condition_id, "closed": "true"})
            data = _get_json(f"{GAMMA}/markets?{q}")
            rows = data if isinstance(data, list) else []
            if rows:
                row = rows[0]
    except (urllib.error.URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        row = None

    _ROW_CACHE[key] = row
    return row


def market_liquidity(*, slug: str | None = None, condition_id: str | None = None) -> float | None:
    """Return a market's liquidity (USD) if found, cached in-process.

    Once a market is fully resolved its order-book liquidity genuinely goes
    to ~0 and Gamma omits the `liquidity`/`liquidityNum` field entirely. In
    that case we fall back to traded `volume` as a liquidity proxy (deep,
    frequently-traded markets are the ones we want to reward) rather than
    reporting a hard zero.
    """
    key = (slug or "") + "|" + (condition_id or "")
    if not key.strip("|"):
        return None
    if key in _CACHE:
        return _CACHE[key]

    row = _market_row(slug=slug, condition_id=condition_id)
    liq: float | None = None
    if row:
        liq = float(row.get("liquidityNum") or row.get("liquidity") or 0)
        if liq <= 0:
            vol = float(row.get("volumeNum") or row.get("volume") or 0)
            liq = vol or None

    if liq is not None:
        _CACHE[key] = liq
    return liq


def market_mark_price(
    *, slug: str | None = None, condition_id: str | None = None, outcome: str | None = None
) -> float | None:
    """Best-effort mark/mid price for a position's outcome.

    Reads Gamma's `outcomes` / `outcomePrices` arrays, which reflect the
    current book mid-price for live markets and collapse to 1.0/0.0 once a
    market resolves — so this doubles as our resolution price source.
    """
    row = _market_row(slug=slug, condition_id=condition_id)
    if not row:
        return None
    try:
        outcomes = json.loads(row.get("outcomes") or "[]")
        prices = json.loads(row.get("outcomePrices") or "[]")
    except (ValueError, TypeError):
        return None
    if not outcomes or not prices or len(outcomes) != len(prices):
        return None
    if outcome:
        for name, price in zip(outcomes, prices):
            if str(name).strip().lower() == str(outcome).strip().lower():
                try:
                    return float(price)
                except (TypeError, ValueError):
                    return None
    try:
        return float(prices[0])
    except (TypeError, ValueError, IndexError):
        return None


def market_is_closed(*, slug: str | None = None, condition_id: str | None = None) -> bool | None:
    row = _market_row(slug=slug, condition_id=condition_id)
    if not row:
        return None
    return bool(row.get("closed"))


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
