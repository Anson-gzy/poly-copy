"""Attribution report: realized PnL / slippage / turnover by source wallet,
domain, and time-to-settlement bucket.

Reads dashboard/ledger.json (+ equity.json). Per-fill detail (source wallet,
domain, slippage, reason) is only available from the `ledger["fills"]` log,
which this codebase started writing alongside this attribution command —
runs before that change have no per-fill record, only the aggregate
`wallet_realized` totals. The report says exactly which case it's in rather
than silently reporting zeros as if they were real.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from poly_copy.ledger import load_equity, load_ledger

TIME_BUCKETS: list[tuple[str, float]] = [
    ("<1h", 3600.0),
    ("1-6h", 6 * 3600.0),
    ("6-24h", 24 * 3600.0),
    (">24h", float("inf")),
]


def _bucket_for(delta_seconds: float) -> str:
    for name, upper in TIME_BUCKETS:
        if delta_seconds < upper:
            return name
    return ">24h"


def _empty_agg() -> dict[str, float]:
    return {"n_fills": 0, "realized_pnl": 0.0, "slippage_cost": 0.0, "notional": 0.0}


def build_attribution(
    *,
    ledger_path: Path | None = None,
    equity_path: Path | None = None,
    fetch_end_dates: bool = True,
) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    equity_pts = load_equity(equity_path)
    fills = list(ledger.get("fills") or [])

    by_wallet: dict[str, dict[str, float]] = {}
    by_domain: dict[str, dict[str, float]] = {}
    total_pnl_fills = 0.0
    total_slippage_cost = 0.0
    time_buckets = {name: {"n": 0, "pnl": 0.0} for name, _ in TIME_BUCKETS}
    unknown_time = {"n": 0, "pnl": 0.0}

    end_date_fn = None
    if fetch_end_dates:
        from poly_copy.liquidity import market_end_date

        end_date_fn = market_end_date

    for f in fills:
        w = str(f.get("source") or "unknown")
        d = str(f.get("domain") or "") or "unknown"
        pnl = float(f.get("pnl") or 0.0)
        notional = float(f.get("notional") or 0.0)
        slip = float(f.get("slippage") or 0.0)
        slip_cost = abs(slip) * notional

        wb = by_wallet.setdefault(w, _empty_agg())
        wb["n_fills"] += 1
        wb["realized_pnl"] += pnl
        wb["slippage_cost"] += slip_cost
        wb["notional"] += notional

        db = by_domain.setdefault(d, _empty_agg())
        db["n_fills"] += 1
        db["realized_pnl"] += pnl
        db["slippage_cost"] += slip_cost
        db["notional"] += notional

        total_pnl_fills += pnl
        total_slippage_cost += slip_cost

        if end_date_fn is not None and str(f.get("side")) == "BUY":
            end_iso = None
            try:
                end_iso = end_date_fn(slug=f.get("market") or None)
            except Exception:
                end_iso = None
            delta = None
            if end_iso:
                try:
                    end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
                    open_dt = datetime.fromtimestamp(float(f.get("ts") or 0), tz=timezone.utc)
                    delta = (end_dt - open_dt).total_seconds()
                except (ValueError, TypeError, OSError):
                    delta = None
            if delta is not None and delta >= 0:
                bucket = _bucket_for(delta)
                time_buckets[bucket]["n"] += 1
                time_buckets[bucket]["pnl"] += pnl
            else:
                unknown_time["n"] += 1
                unknown_time["pnl"] += pnl

    evict_fills = [f for f in fills if f.get("reason") == "evict_universe_churn"]
    turnover_pnl = sum(float(f.get("pnl") or 0.0) for f in evict_fills)

    equity_start = float(equity_pts[0]["equity"]) if equity_pts else float(ledger.get("initial_capital") or 1000.0)
    equity_now = float(equity_pts[-1]["equity"]) if equity_pts else float(ledger.get("cash") or 0.0)
    total_pnl_ledger = equity_now - equity_start

    wallet_realized = {k: float(v) for k, v in (ledger.get("wallet_realized") or {}).items()}

    slippage_share_of_loss = None
    if total_pnl_ledger < 0 and total_slippage_cost > 0:
        slippage_share_of_loss = round(min(1.0, total_slippage_cost / abs(total_pnl_ledger)), 4)

    # ledger['wallet_realized'] is the authoritative *cumulative* realized PnL
    # per wallet (booked on every SELL/settle/evict since the ledger began);
    # the fills log only exists from this change onward, so it may cover just
    # a fraction of that history. Report the real cumulative PnL per wallet
    # always, and layer on whatever fill-level slippage/domain detail exists.
    all_wallets = set(wallet_realized) | set(by_wallet)
    wallet_report = {}
    for w in all_wallets:
        v = by_wallet.get(w)
        entry: dict[str, Any] = {
            "realized_pnl_cumulative": round(wallet_realized.get(w, 0.0), 4),
        }
        if v:
            entry.update(
                {
                    "n_fills_logged": int(v["n_fills"]),
                    "realized_pnl_in_logged_fills": round(v["realized_pnl"], 4),
                    "slippage_cost_in_logged_fills": round(v["slippage_cost"], 4),
                    "avg_slippage_in_logged_fills": (
                        round(v["slippage_cost"] / v["notional"], 5) if v["notional"] else 0.0
                    ),
                    "notional_in_logged_fills": round(v["notional"], 2),
                }
            )
        else:
            entry["note"] = "no fills logged yet for this wallet — pnl is the pre-fix aggregate"
        wallet_report[w] = entry
    wallet_report = dict(
        sorted(wallet_report.items(), key=lambda kv: kv[1]["realized_pnl_cumulative"])
    )

    if by_domain:
        domain_report: dict[str, Any] = {
            d: {
                "n_fills": int(v["n_fills"]),
                "realized_pnl": round(v["realized_pnl"], 4),
                "slippage_cost": round(v["slippage_cost"], 4),
                "notional": round(v["notional"], 2),
                "pnl_share_of_total": (
                    round(v["realized_pnl"] / total_pnl_fills, 4) if total_pnl_fills else None
                ),
            }
            for d, v in sorted(by_domain.items(), key=lambda kv: kv[1]["realized_pnl"])
        }
    else:
        domain_report = {
            "_note": "no per-fill domain tag recorded for this period — the fills log "
            "(ledger['fills'], with domain/slippage/reason) was added by this change and "
            "starts accumulating from now"
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "wallet_realized_cumulative+fills_detail" if fills else "wallet_realized_cumulative_only",
        "fills_coverage_note": (
            "per-wallet realized_pnl_cumulative is the full authoritative history from "
            "ledger['wallet_realized']; slippage/domain/time-to-settlement detail only covers "
            f"the {len(fills)} fill(s) logged since the fills log was added — not the full "
            "6-day pre-fix history, which has no per-fill record"
            if fills
            else "no fills logged yet; only cumulative per-wallet PnL is available"
        ),
        "n_fills_logged": len(fills),
        "equity_start": round(equity_start, 4),
        "equity_now": round(equity_now, 4),
        "total_pnl": round(total_pnl_ledger, 4),
        "by_source_wallet": wallet_report,
        "by_domain": domain_report,
        "slippage": {
            "total_cost": round(total_slippage_cost, 4),
            "share_of_total_loss": slippage_share_of_loss,
            "note": None if fills else "per-fill slippage not recoverable for the pre-fix period",
        },
        "time_to_settlement": (
            {name: {"n": b["n"], "pnl": round(b["pnl"], 4)} for name, b in time_buckets.items()}
            if fills
            else {"_note": "no fills logged for this period"}
        ),
        "time_to_settlement_unknown": {"n": unknown_time["n"], "pnl": round(unknown_time["pnl"], 4)},
        "turnover_cost_from_universe_churn": {
            "realized_pnl": round(turnover_pnl, 4),
            "n_evict_fills_logged": len(evict_fills),
            "n_raw_eviction_records": len(ledger.get("evicted") or []),
            "note": (
                None
                if evict_fills
                else "universe-churn eviction pnl exists in ledger['evicted'] (raw closed_positions "
                "records) but predates per-fill reason tagging, so it can't be broken out by "
                "domain/slippage here; new evictions are logged with reason=evict_universe_churn"
            ),
        },
    }
