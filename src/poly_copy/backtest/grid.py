"""`poly-copy backtest --grid`: parameter-grid replay over real trade history.

vectorbt-style output (one row of metrics per parameter combo) without the
vectorbt dependency — the grid here is small (4×3×3×3 = 108 combos) so a
plain Python loop over cached event data is simpler and correct.

Approximations, called out explicitly because there's no tick-by-tick book:
- "settlement filter" skips a BUY if its market's Gamma `endDate` is closer
  than the threshold at the time of the trade (skip if unknown — conservative).
- "delay" doesn't replay a real order book at t+delay; it adds a flat slippage
  penalty (0 / 1% / 2.5%) to the configured slippage_bps as a stand-in for the
  extra adverse move a slower fill would have eaten. This is a proxy, not a
  simulation of book depth at a later timestamp.
- max_drawdown is computed off *realized* PnL only (walking the fill log),
  not mark-to-market on open positions between fills — we don't have a full
  intraday price series to mark against. total_pnl / final_equity, by
  contrast, mark open positions at the *current* live price, so they reflect
  today's real unrealized state.
"""

from __future__ import annotations

import copy as _copy
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from poly_copy.ledger import ingest_events, new_ledger, settle_resolved
from poly_copy.liquidity import market_end_date, market_is_closed, market_mark_price
from poly_copy.types import WalletEvent

DATA = "https://data-api.polymarket.com"
_UA = {"accept": "application/json", "user-agent": "Mozilla/5.0 (compatible; poly-copy/0.1)"}

# delay tier (seconds) -> extra slippage penalty in bps, applied on top of
# the configured copy.slippage_bps (see module docstring: this is a proxy
# for "the market moved against us while we were slow to fill", not a replay
# of an actual future order book).
DELAY_SLIPPAGE_BPS: dict[int, float] = {0: 0.0, 60: 100.0, 300: 250.0}

DEFAULT_FIXED_NOTIONAL_CAPS = (10.0, 20.0, 25.0, 50.0)
DEFAULT_STOP_LOSSES = (0.5, 0.7, 0.85)
DEFAULT_SETTLEMENT_FILTERS_HOURS = (0.0, 2.0, 6.0)
DEFAULT_DELAY_SECONDS = (0, 60, 300)


def _get(url: str, timeout: float = 10.0, retries: int = 2) -> Any:
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == retries:
                return None
            time.sleep(0.5 * (2**attempt))
    return None


def fetch_wallet_events(wallet: str, *, limit: int = 300) -> list[WalletEvent]:
    """Re-pull one wallet's recent trade history from the public data API."""
    url = f"{DATA}/trades?{urllib.parse.urlencode({'user': wallet, 'limit': limit})}"
    trades = _get(url) or []
    out: list[WalletEvent] = []
    for t in trades if isinstance(trades, list) else []:
        ts = float(t.get("timestamp") or 0)
        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        out.append(
            WalletEvent(
                address=wallet,
                side=str(t.get("side") or "BUY").upper(),
                size=size,
                price=price,
                notional=size * price,
                market=str(t.get("slug") or t.get("title") or ""),
                event_slug=str(t.get("eventSlug") or ""),
                outcome=str(t.get("outcome") or ""),
                timestamp=datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else None,
                tx_hash=t.get("transactionHash"),
                condition_id=t.get("conditionId"),
                token_id=str(t.get("asset") or "") or None,
            )
        )
    return out


def gather_events(wallets: list[str], *, limit: int = 300) -> list[WalletEvent]:
    events: list[WalletEvent] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch_wallet_events, w, limit=limit): w for w in wallets}
        for fut in as_completed(futs):
            events.extend(fut.result())
    events.sort(key=lambda e: e.timestamp or datetime.fromtimestamp(0, tz=timezone.utc))
    return events


def _domain_of(ev: WalletEvent) -> str:
    from poly_copy.features import _domain_key

    return _domain_key({"event_slug": ev.event_slug, "slug": ev.market})


def build_end_date_cache(events: list[WalletEvent]) -> dict[str, str | None]:
    keys = sorted({ev.market for ev in events if ev.market})
    out: dict[str, str | None] = {}

    def one(slug: str) -> tuple[str, str | None]:
        try:
            return slug, market_end_date(slug=slug)
        except Exception:
            return slug, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(one, k) for k in keys]
        for fut in as_completed(futs):
            k, v = fut.result()
            out[k] = v
    return out


@dataclass
class GridResult:
    fixed_notional_cap: float
    stop_loss: float
    settlement_filter_hours: float
    delay_seconds: float
    total_pnl: float
    max_drawdown: float
    n_fills: int
    n_skipped_settlement_filter: int
    slippage_cost: float
    final_equity: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixed_notional_cap": self.fixed_notional_cap,
            "stop_loss": self.stop_loss,
            "settlement_filter_hours": self.settlement_filter_hours,
            "delay_seconds": self.delay_seconds,
            "total_pnl": round(self.total_pnl, 4),
            "max_dd": round(self.max_drawdown, 4),
            "n_fills": self.n_fills,
            "n_skipped_settlement_filter": self.n_skipped_settlement_filter,
            "slippage_cost": round(self.slippage_cost, 4),
            "final_equity": round(self.final_equity, 4),
        }


def _filter_by_settlement(
    events: list[WalletEvent],
    *,
    settlement_filter_hours: float,
    end_dates: dict[str, str | None],
) -> tuple[list[WalletEvent], int]:
    if settlement_filter_hours <= 0:
        return events, 0
    kept: list[WalletEvent] = []
    skipped = 0
    for ev in events:
        if ev.side.upper() != "BUY":
            kept.append(ev)
            continue
        end_iso = end_dates.get(ev.market)
        if not end_iso:
            # unknown endDate: conservative default is to keep the trade
            # rather than silently dropping data we can't evaluate.
            kept.append(ev)
            continue
        try:
            end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
            ev_dt = ev.timestamp or datetime.fromtimestamp(0, tz=timezone.utc)
            hours_left = (end_dt - ev_dt).total_seconds() / 3600.0
        except (ValueError, TypeError):
            kept.append(ev)
            continue
        if hours_left < settlement_filter_hours:
            skipped += 1
            continue
        kept.append(ev)
    return kept, skipped


def run_grid_combo(
    events: list[WalletEvent],
    alloc: dict[str, float],
    base_cfg: dict[str, Any],
    *,
    fixed_notional_cap: float,
    stop_loss: float,
    settlement_filter_hours: float,
    delay_seconds: float,
    end_dates: dict[str, str | None],
    initial_capital: float = 1000.0,
) -> GridResult:
    cfg = _copy.deepcopy(base_cfg)
    risk = cfg.setdefault("risk", {})
    risk["per_trade_cap"] = float(fixed_notional_cap)
    risk["per_trade_stop_loss"] = float(stop_loss)
    copy_cfg = cfg.setdefault("copy", {})
    base_bps = float(copy_cfg.get("slippage_bps", 500))
    copy_cfg["slippage_bps"] = base_bps + DELAY_SLIPPAGE_BPS.get(int(delay_seconds), 0.0)

    filtered, n_skipped = _filter_by_settlement(
        events, settlement_filter_hours=settlement_filter_hours, end_dates=end_dates
    )

    ledger = new_ledger(initial_capital)
    ingest_events(
        ledger,
        filtered,
        alloc=alloc,
        cfg=cfg,
        liquidity_ok=lambda _ev: True,  # offline replay: no live book to re-check
        domain_fn=_domain_of,
    )
    settle_resolved(ledger, is_closed_fn=market_is_closed, price_fn=market_mark_price)

    # mark remaining open positions at the current live price for the final
    # equity figure (accurate for "today"); max_dd below only sees realized
    # PnL because we don't have a historical mark series between fills.
    pv = 0.0
    for pos in ledger["positions"].values():
        price = market_mark_price(
            slug=pos.get("market") or None,
            condition_id=pos.get("condition_id"),
            outcome=pos.get("outcome") or None,
        )
        pv += float(pos["size"]) * float(price if price is not None else pos["avg_price"])
    final_equity = float(ledger["cash"]) + pv

    fills = ledger.get("fills") or []
    running = initial_capital
    peak = running
    max_dd = 0.0
    for f in fills:
        running += float(f.get("pnl") or 0.0)
        peak = max(peak, running)
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak)

    slippage_cost = sum(abs(float(f.get("slippage") or 0)) * float(f.get("notional") or 0) for f in fills)

    return GridResult(
        fixed_notional_cap=fixed_notional_cap,
        stop_loss=stop_loss,
        settlement_filter_hours=settlement_filter_hours,
        delay_seconds=delay_seconds,
        total_pnl=final_equity - initial_capital,
        max_drawdown=max_dd,
        n_fills=len(fills),
        n_skipped_settlement_filter=n_skipped,
        slippage_cost=slippage_cost,
        final_equity=final_equity,
    )


def default_grid_wallets() -> tuple[list[str], dict[str, float]]:
    """Universe members (current) unioned with any wallet ever seen in the
    ledger (cursors / wallet_realized) — the best available proxy for "who we
    were actually following" over the accumulated paper session, since the
    universe churned across the window and only its current snapshot persists."""
    from poly_copy.ledger import load_ledger
    from poly_copy.universe import load_universe

    uni = load_universe()
    alloc = {str(k).lower(): float(v) for k, v in (uni.get("allocation") or {}).items()}
    wallets = set(alloc)

    ledger = load_ledger()
    wallets |= {str(w).lower() for w in (ledger.get("wallet_realized") or {}).keys()}
    wallets |= {str(w).lower() for w in (ledger.get("cursors") or {}).keys()}

    wallets_list = sorted(wallets)
    if not alloc and wallets_list:
        alloc = {w: 1.0 / len(wallets_list) for w in wallets_list}
    elif alloc:
        # wallets outside the current allocation (evicted mid-window) still
        # contributed history; fold them in at a small equal share so the
        # grid can see their PnL/slippage too.
        extra = [w for w in wallets_list if w not in alloc]
        if extra:
            leftover = 0.05
            for w in extra:
                alloc[w] = leftover / len(extra)
            total = sum(alloc.values())
            alloc = {w: v / total for w, v in alloc.items()}
    return wallets_list, alloc


def run_grid(
    cfg: dict[str, Any],
    *,
    wallets: list[str] | None = None,
    alloc: dict[str, float] | None = None,
    fixed_notional_caps: tuple[float, ...] = DEFAULT_FIXED_NOTIONAL_CAPS,
    stop_losses: tuple[float, ...] = DEFAULT_STOP_LOSSES,
    settlement_filters_hours: tuple[float, ...] = DEFAULT_SETTLEMENT_FILTERS_HOURS,
    delay_seconds_list: tuple[float, ...] = DEFAULT_DELAY_SECONDS,
    trade_limit: int = 300,
    initial_capital: float = 1000.0,
) -> dict[str, Any]:
    if wallets is None or alloc is None:
        wallets, alloc = default_grid_wallets()
    if not wallets:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "error": "no wallets found in universe/ledger to replay",
            "grid": [],
        }

    events = gather_events(wallets, limit=trade_limit)
    end_dates = build_end_date_cache(events)

    results: list[GridResult] = []
    for cap in fixed_notional_caps:
        for sl in stop_losses:
            for filt in settlement_filters_hours:
                for delay in delay_seconds_list:
                    results.append(
                        run_grid_combo(
                            events,
                            alloc,
                            cfg,
                            fixed_notional_cap=cap,
                            stop_loss=sl,
                            settlement_filter_hours=filt,
                            delay_seconds=delay,
                            end_dates=end_dates,
                            initial_capital=initial_capital,
                        )
                    )
    results.sort(key=lambda r: r.total_pnl, reverse=True)

    by_delay: dict[str, dict[str, float]] = {}
    for d in delay_seconds_list:
        rows = [r for r in results if r.delay_seconds == d]
        pnls = [r.total_pnl for r in rows]
        by_delay[str(int(d))] = {
            "n": len(rows),
            "n_positive": sum(1 for p in pnls if p > 0),
            "best_pnl": max(pnls) if pnls else None,
            "median_pnl": sorted(pnls)[len(pnls) // 2] if pnls else None,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "caveats": [
            "survivorship/hindsight bias: replayed wallets are the CURRENT universe/ledger "
            "wallets — i.e. the winners that survived churn — and their BUYs are settled at "
            "now-known outcomes; absolute PnL is therefore optimistic and NOT comparable to "
            "the live paper result. Use this grid for RELATIVE comparisons (delay sensitivity, "
            "settlement filter, notional cap) only.",
            "stop_loss dimension is inert in this replay: positions only revalue at "
            "settlement/current mark (no intra-trade price series), so a per-trade stop "
            "never triggers and results are identical across stop_loss values.",
            "delay is approximated as extra flat slippage (0/1%/2.5%), not an order-book "
            "replay at t+delay.",
        ],
        "wallets": wallets,
        "n_events": len(events),
        "grid": [r.to_dict() for r in results],
        "best": results[0].to_dict() if results else None,
        "worst": results[-1].to_dict() if results else None,
        "n_positive": sum(1 for r in results if r.total_pnl > 0),
        "n_total": len(results),
        "by_delay_seconds": by_delay,
    }
