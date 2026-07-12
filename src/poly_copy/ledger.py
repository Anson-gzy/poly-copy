"""Persistent paper ledger: positions, per-wallet cursors, equity history.

Files (committed by CI so state accumulates across runs):
- dashboard/ledger.json  — cash, open positions, per-source-wallet cursors,
  realized PnL, halt flag, eviction log.
- dashboard/equity.json  — append-only equity curve (rolling truncation).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from poly_copy.config import PACKAGE_ROOT
from poly_copy.types import WalletEvent

DEFAULT_INITIAL_CAPITAL = 1000.0
EQUITY_MAX_POINTS = 2000

PriceFn = Callable[..., "float | None"]  # (slug=, condition_id=, outcome=) -> price
ClosedFn = Callable[..., "bool | None"]  # (slug=, condition_id=) -> closed?


def default_ledger_path() -> Path:
    return PACKAGE_ROOT / "dashboard" / "ledger.json"


def default_equity_path() -> Path:
    return PACKAGE_ROOT / "dashboard" / "equity.json"


def new_ledger(initial_capital: float = DEFAULT_INITIAL_CAPITAL) -> dict[str, Any]:
    return {
        "initial_capital": float(initial_capital),
        "cash": float(initial_capital),
        "realized_pnl_cum": 0.0,
        "halted": False,
        "halt_reason": "",
        "high_water": float(initial_capital),
        # position key -> {token_id, size, avg_price, source_wallet, market,
        #                  outcome, condition_id, domain, opened_at,
        #                  last_mark, mark_stale}
        "positions": {},
        # source wallet -> {"ts": epoch_float, "tx": [hashes at that ts]}
        "cursors": {},
        "wallet_realized": {},
        "wallet_peak": {},
        "evicted": [],
        "updated_at": None,
    }


def load_ledger(path: Path | None = None) -> dict[str, Any]:
    p = path or default_ledger_path()
    if not p.exists():
        return new_ledger()
    data = json.loads(p.read_text(encoding="utf-8"))
    base = new_ledger(float(data.get("initial_capital") or DEFAULT_INITIAL_CAPITAL))
    base.update(data)
    return base


def save_ledger(ledger: dict[str, Any], path: Path | None = None) -> Path:
    p = path or default_ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger["updated_at"] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(ledger, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def load_equity(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or default_equity_path()
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def save_equity(points: list[dict[str, Any]], path: Path | None = None) -> Path:
    p = path or default_equity_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(points, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def append_equity_point(
    points: list[dict[str, Any]],
    *,
    ts: str,
    equity: float,
    cash: float,
    positions_value: float,
    n_open: int,
    realized_pnl_cum: float,
    max_points: int = EQUITY_MAX_POINTS,
) -> list[dict[str, Any]]:
    points.append(
        {
            "ts": ts,
            "equity": round(equity, 4),
            "cash": round(cash, 4),
            "positions_value": round(positions_value, 4),
            "n_open": n_open,
            "realized_pnl_cum": round(realized_pnl_cum, 4),
        }
    )
    if len(points) > max_points:
        del points[: len(points) - max_points]
    return points


# ---------------------------------------------------------------------------
# sizing


def copy_notional(
    *,
    weight: float,
    equity: float,
    leader_notional: float,
    leader_portfolio_value: float | None,
    per_trade_cap: float = 50.0,
) -> float:
    """Follow size = min(weight × equity × leader's own portfolio share, cap).

    When the leader's portfolio value is unknown, assume share=1 so the
    per-trade cap dominates (conservative but never zero).
    """
    if weight <= 0 or equity <= 0 or leader_notional <= 0:
        return 0.0
    if leader_portfolio_value and leader_portfolio_value > 0:
        share = min(1.0, leader_notional / leader_portfolio_value)
    else:
        share = 1.0
    return min(weight * equity * share, per_trade_cap)


def _position_key(ev: WalletEvent) -> str:
    if ev.token_id:
        return str(ev.token_id)
    return f"{ev.condition_id or ev.market}:{ev.outcome}"


def _event_epoch(ev: WalletEvent) -> float:
    if ev.timestamp is None:
        return 0.0
    return ev.timestamp.timestamp()


def wallet_exposure(ledger: dict[str, Any], wallet: str) -> float:
    """Current cost-basis exposure attributed to one source wallet."""
    total = 0.0
    for pos in ledger["positions"].values():
        if str(pos.get("source_wallet", "")).lower() == wallet.lower():
            total += float(pos["size"]) * float(pos["avg_price"])
    return total


def current_equity(ledger: dict[str, Any]) -> float:
    pv = sum(
        float(p["size"]) * float(p.get("last_mark") or p["avg_price"])
        for p in ledger["positions"].values()
    )
    return float(ledger["cash"]) + pv


def ingest_events(
    ledger: dict[str, Any],
    events: list[WalletEvent],
    *,
    alloc: dict[str, float],
    cfg: dict[str, Any],
    liquidity_ok: Callable[[WalletEvent], bool] | None = None,
    leader_values: dict[str, float] | None = None,
    domain_fn: Callable[[WalletEvent], str] | None = None,
) -> dict[str, Any]:
    """Apply only events newer than each wallet's cursor; update positions.

    Returns a summary: n_events / n_new / n_buys / n_sells / n_skipped.
    When ledger['halted'] is true, only SELL events are processed.
    """
    risk = cfg.get("risk", {})
    slip = float(cfg.get("copy", {}).get("slippage_bps", 500)) / 10000.0
    per_trade_cap = float(risk.get("per_trade_cap", 50.0))
    exposure_cap = float(risk.get("wallet_exposure_cap", 0.10))
    alloc_norm = {a.lower(): float(w) for a, w in alloc.items()}
    leader_values = {k.lower(): v for k, v in (leader_values or {}).items()}

    summary = {
        "n_events": len(events),
        "n_new": 0,
        "n_buys": 0,
        "n_sells": 0,
        "n_skipped_stale": 0,
        "n_skipped_halted": 0,
        "n_skipped_liquidity": 0,
        "n_skipped_other": 0,
        "fills": [],
    }

    for ev in sorted(events, key=_event_epoch):
        w = ev.address.lower()
        ts = _event_epoch(ev)
        tx = ev.tx_hash or f"{ev.market}:{ev.side}:{ev.size}:{ts}"
        cur = ledger["cursors"].get(w) or {"ts": 0.0, "tx": []}
        if ts < cur["ts"] or (ts == cur["ts"] and tx in cur["tx"]):
            summary["n_skipped_stale"] += 1
            continue
        # advance cursor (event counts as seen even if not copied)
        if ts > cur["ts"]:
            cur = {"ts": ts, "tx": [tx]}
        else:
            cur["tx"].append(tx)
        ledger["cursors"][w] = cur
        summary["n_new"] += 1

        weight = alloc_norm.get(w, 0.0)
        if weight <= 0:
            summary["n_skipped_other"] += 1
            continue

        side = ev.side.upper()
        key = _position_key(ev)
        price = ev.price if ev.price > 0 else 0.5

        if side == "BUY":
            if ledger["halted"]:
                summary["n_skipped_halted"] += 1
                continue
            if liquidity_ok is not None and not liquidity_ok(ev):
                summary["n_skipped_liquidity"] += 1
                continue
            equity = current_equity(ledger)
            notional = copy_notional(
                weight=weight,
                equity=equity,
                leader_notional=ev.notional,
                leader_portfolio_value=leader_values.get(w),
                per_trade_cap=per_trade_cap,
            )
            # single source wallet total exposure <= exposure_cap * equity
            room = exposure_cap * equity - wallet_exposure(ledger, w)
            notional = min(notional, max(0.0, room), max(0.0, ledger["cash"]))
            if notional < 1.0:
                summary["n_skipped_other"] += 1
                continue
            fill_price = min(0.999, price * (1.0 + slip))
            size = notional / fill_price
            pos = ledger["positions"].get(key)
            if pos:
                tot = float(pos["size"]) + size
                pos["avg_price"] = (
                    float(pos["size"]) * float(pos["avg_price"]) + size * fill_price
                ) / tot
                pos["size"] = tot
            else:
                ledger["positions"][key] = {
                    "token_id": ev.token_id,
                    "size": size,
                    "avg_price": fill_price,
                    "source_wallet": w,
                    "market": ev.market,
                    "outcome": ev.outcome,
                    "condition_id": ev.condition_id,
                    "domain": domain_fn(ev) if domain_fn else "",
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                    "last_mark": fill_price,
                    "mark_stale": False,
                }
            ledger["cash"] -= size * fill_price
            summary["n_buys"] += 1
            summary["fills"].append(
                {"side": "BUY", "market": ev.market, "outcome": ev.outcome,
                 "size": round(size, 4), "price": round(fill_price, 4),
                 "notional": round(notional, 2), "source": w}
            )
        elif side == "SELL":
            pos = ledger["positions"].get(key)
            if not pos:
                summary["n_skipped_other"] += 1
                continue
            fill_price = max(0.001, price * (1.0 - slip))
            target_notional = copy_notional(
                weight=weight,
                equity=current_equity(ledger),
                leader_notional=ev.notional,
                leader_portfolio_value=leader_values.get(w),
                per_trade_cap=per_trade_cap,
            )
            sell_size = min(float(pos["size"]), target_notional / fill_price)
            if sell_size <= 0:
                summary["n_skipped_other"] += 1
                continue
            realized = (fill_price - float(pos["avg_price"])) * sell_size
            _book_realized(ledger, w, realized)
            ledger["cash"] += sell_size * fill_price
            pos["size"] = float(pos["size"]) - sell_size
            if pos["size"] * float(pos["avg_price"]) < 0.01:
                # flush dust at the same fill price
                dust = float(pos["size"])
                if dust > 0:
                    _book_realized(ledger, w, (fill_price - float(pos["avg_price"])) * dust)
                    ledger["cash"] += dust * fill_price
                del ledger["positions"][key]
            summary["n_sells"] += 1
            summary["fills"].append(
                {"side": "SELL", "market": ev.market, "outcome": ev.outcome,
                 "size": round(sell_size, 4), "price": round(fill_price, 4),
                 "pnl": round(realized, 4), "source": w}
            )
        else:
            summary["n_skipped_other"] += 1
    return summary


def _book_realized(ledger: dict[str, Any], wallet: str, realized: float) -> None:
    ledger["realized_pnl_cum"] = float(ledger["realized_pnl_cum"]) + realized
    wr = ledger["wallet_realized"]
    wr[wallet] = float(wr.get(wallet, 0.0)) + realized
    wp = ledger["wallet_peak"]
    wp[wallet] = max(float(wp.get(wallet, 0.0)), wr[wallet])


# ---------------------------------------------------------------------------
# settlement / marking / risk


def settle_resolved(
    ledger: dict[str, Any],
    *,
    is_closed_fn: ClosedFn,
    price_fn: PriceFn,
) -> list[dict[str, Any]]:
    """Convert positions in resolved markets into realized PnL at settlement price."""
    settled: list[dict[str, Any]] = []
    for key in list(ledger["positions"].keys()):
        pos = ledger["positions"][key]
        closed = is_closed_fn(slug=pos.get("market") or None, condition_id=pos.get("condition_id"))
        if not closed:
            continue
        price = price_fn(
            slug=pos.get("market") or None,
            condition_id=pos.get("condition_id"),
            outcome=pos.get("outcome") or None,
        )
        if price is None:
            price = float(pos.get("last_mark") or pos["avg_price"])
        realized = (price - float(pos["avg_price"])) * float(pos["size"])
        _book_realized(ledger, str(pos.get("source_wallet", "")), realized)
        ledger["cash"] += float(pos["size"]) * price
        settled.append({"key": key, "market": pos.get("market"), "settle_price": price,
                        "pnl": round(realized, 4)})
        del ledger["positions"][key]
    return settled


def mark_positions(ledger: dict[str, Any], *, price_fn: PriceFn) -> float:
    """Mark open positions to current mid; keep last mark + stale flag when unknown."""
    total = 0.0
    for pos in ledger["positions"].values():
        price = price_fn(
            slug=pos.get("market") or None,
            condition_id=pos.get("condition_id"),
            outcome=pos.get("outcome") or None,
        )
        if price is None:
            pos["mark_stale"] = True
            price = float(pos.get("last_mark") or pos["avg_price"])
        else:
            pos["mark_stale"] = False
            pos["last_mark"] = price
        total += float(pos["size"]) * price
    return total


def update_equity_and_halt(
    ledger: dict[str, Any],
    positions_value: float,
    *,
    halt_drawdown: float = 0.15,
) -> float:
    """Refresh high-water mark; set halted=true on >=halt_drawdown from peak.

    halted is sticky: clearing it requires manually editing ledger.json.
    """
    equity = float(ledger["cash"]) + positions_value
    ledger["high_water"] = max(float(ledger["high_water"]), equity)
    hw = float(ledger["high_water"])
    if hw > 0 and not ledger["halted"]:
        dd = (hw - equity) / hw
        if dd >= halt_drawdown:
            ledger["halted"] = True
            ledger["halt_reason"] = f"portfolio_dd:{dd:.3f}"
    return equity


def wallet_contribution(ledger: dict[str, Any], wallet: str) -> float:
    """Realized + unrealized PnL contributed by one source wallet."""
    w = wallet.lower()
    contrib = float(ledger["wallet_realized"].get(w, 0.0))
    for pos in ledger["positions"].values():
        if str(pos.get("source_wallet", "")).lower() == w:
            mark = float(pos.get("last_mark") or pos["avg_price"])
            contrib += (mark - float(pos["avg_price"])) * float(pos["size"])
    return contrib


def evict_drawdown_wallets(
    ledger: dict[str, Any],
    *,
    max_dd: float = 0.20,
    price_fn: PriceFn | None = None,
) -> list[dict[str, Any]]:
    """Kick wallets whose contributed PnL has drawn down >max_dd since we copied them.

    Drawdown = (peak_contribution - contribution) / initial_capital, i.e. how
    much of our starting stake that wallet has given back from its peak.
    Their paper positions are closed at the current mark.
    """
    cap = float(ledger["initial_capital"]) or DEFAULT_INITIAL_CAPITAL
    wallets = {str(p.get("source_wallet", "")).lower() for p in ledger["positions"].values()}
    wallets |= {w.lower() for w in ledger["wallet_realized"]}
    evicted: list[dict[str, Any]] = []
    already = {e["wallet"] for e in ledger["evicted"]}
    for w in sorted(wallets):
        if not w or w in already:
            continue
        # mark unrealized before judging
        if price_fn is not None:
            for pos in ledger["positions"].values():
                if str(pos.get("source_wallet", "")).lower() != w:
                    continue
                price = price_fn(
                    slug=pos.get("market") or None,
                    condition_id=pos.get("condition_id"),
                    outcome=pos.get("outcome") or None,
                )
                if price is not None:
                    pos["last_mark"] = price
                    pos["mark_stale"] = False
        contrib = wallet_contribution(ledger, w)
        peak = max(float(ledger["wallet_peak"].get(w, 0.0)), 0.0)
        dd = (peak - contrib) / cap
        if dd <= max_dd:
            continue
        # close its positions at mark
        closed = []
        for key in list(ledger["positions"].keys()):
            pos = ledger["positions"][key]
            if str(pos.get("source_wallet", "")).lower() != w:
                continue
            mark = float(pos.get("last_mark") or pos["avg_price"])
            realized = (mark - float(pos["avg_price"])) * float(pos["size"])
            _book_realized(ledger, w, realized)
            ledger["cash"] += float(pos["size"]) * mark
            closed.append({"key": key, "market": pos.get("market"), "pnl": round(realized, 4)})
            del ledger["positions"][key]
        record = {
            "wallet": w,
            "reason": f"wallet_dd:{dd:.3f}",
            "ts": datetime.now(timezone.utc).isoformat(),
            "closed_positions": closed,
        }
        ledger["evicted"].append(record)
        evicted.append(record)
    return evicted
