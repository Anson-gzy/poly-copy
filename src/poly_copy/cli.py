"""CLI: poly-copy screen|score|paper|report|backtest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from poly_copy.backtest import param_scan, run_backtest
from poly_copy.config import PACKAGE_ROOT, load_config
from poly_copy.data import WalletStore, default_store, fetch_wallet
from poly_copy.features import compute_features
from poly_copy.portfolio import allocate, domain_dispersion
from poly_copy.risk import detect_drift
from poly_copy.score import rank_universe, score_wallet
from poly_copy.types import Allocation


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _wallets_from_args(args: argparse.Namespace, cfg: dict[str, Any]) -> list[str]:
    if getattr(args, "wallets", None):
        return [w.strip().lower() for w in args.wallets if w.strip()]
    if getattr(args, "wallet", None):
        return [args.wallet.strip().lower()]
    use_universe = getattr(args, "universe", None)
    if use_universe is not False:
        from poly_copy.universe import universe_wallets

        ws = universe_wallets()
        if ws:
            return ws
        if use_universe is True:
            raise SystemExit("universe is empty; run: poly-copy universe sync")
    return [str(cfg.get("case_wallet", "")).lower()]


def cmd_universe(args: argparse.Namespace) -> int:
    from poly_copy.universe import load_universe, sync_universe

    cfg = load_config(args.config)
    if args.action == "show":
        _print(load_universe())
        return 0
    # sync
    state = sync_universe(cfg, refresh=not args.no_refresh)
    _print(state)
    if state.get("shortfall", 0) > 0:
        print(
            f"warning: only {state.get('active_n')} / {state.get('target_n')} suitable wallets",
            file=sys.stderr,
        )
    return 0


def _load_snaps(
    wallets: list[str],
    store: WalletStore,
    cfg: dict[str, Any],
    refresh: bool,
):
    snaps = []
    for w in wallets:
        try:
            snaps.append(store.get_or_fetch(w, refresh=refresh, cfg=cfg))
        except Exception as e:
            print(f"skip_wallet {w}: {e}", file=sys.stderr)
    if not snaps:
        raise SystemExit("no wallet snapshots loaded")
    return snaps


def cmd_screen(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = default_store(cfg)
    wallets = _wallets_from_args(args, cfg)
    snaps = _load_snaps(wallets, store, cfg, args.refresh)
    rows = []
    for snap in snaps:
        feat = compute_features(snap)
        sc = score_wallet(feat, cfg)
        rows.append(
            {
                "features": feat.to_dict(),
                "score": sc.to_dict(),
                "verdict": "适合跟" if sc.suitable else f"不适合跟 ({sc.hard_reject_reason})",
            }
        )
    _print({"wallets": rows})
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = default_store(cfg)
    wallets = _wallets_from_args(args, cfg)
    snaps = _load_snaps(wallets, store, cfg, args.refresh)
    features = [compute_features(s) for s in snaps]
    ranked = rank_universe(features, cfg)
    alloc = allocate(ranked, cfg)
    _print(
        {
            "ranking": [
                {"features": f.to_dict(), "score": s.to_dict()} for f, s in ranked
            ],
            "allocation": alloc,
        }
    )
    return 0


def _bootstrap_cursors(ledger: dict[str, Any], wallets: list[str], cfg: dict[str, Any]) -> None:
    """Cold start: point missing cursors a short lookback into the past so a
    fresh ledger does not replay months of history."""
    import time as _time

    lookback_h = float(cfg.get("ledger", {}).get("bootstrap_lookback_hours", 24))
    floor_ts = _time.time() - lookback_h * 3600.0
    for w in wallets:
        if w.lower() not in ledger["cursors"]:
            ledger["cursors"][w.lower()] = {"ts": floor_ts, "tx": []}


def _fetch_leader_values(wallets: list[str]) -> dict[str, float]:
    """Leader portfolio values (USD) so follow size can scale by the leader's
    own portfolio share. Missing values fall back to per-trade-cap sizing."""
    import urllib.parse
    import urllib.request

    out: dict[str, float] = {}
    for w in wallets:
        url = f"https://data-api.polymarket.com/value?{urllib.parse.urlencode({'user': w})}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    url,
                    headers={
                        "accept": "application/json",
                        "user-agent": "Mozilla/5.0 (compatible; poly-copy/0.1)",
                    },
                ),
                timeout=10,
            ) as resp:
                vals = json.loads(resp.read().decode())
            if isinstance(vals, list) and vals:
                v = float(vals[0].get("value") or 0)
                if v > 0:
                    out[w.lower()] = v
        except Exception as e:
            print(f"leader_value_error {w}: {e}", file=sys.stderr)
    return out


def _ledger_cycle(
    ledger: dict[str, Any],
    events: list,
    alloc: dict[str, float],
    cfg: dict[str, Any],
    *,
    leader_values: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Shared incremental update: ingest → settle → evict → mark → equity → save."""
    from datetime import datetime, timezone

    from poly_copy.features import _domain_key
    from poly_copy.ledger import (
        append_equity_point,
        evict_drawdown_wallets,
        ingest_events,
        load_equity,
        mark_positions,
        save_equity,
        save_ledger,
        settle_resolved,
        update_equity_and_halt,
    )
    from poly_copy.liquidity import (
        market_is_closed,
        market_liquidity,
        market_mark_price,
        trade_ok,
    )
    from poly_copy.universe import evict_member

    risk = cfg.get("risk", {})

    def liq_ok(ev) -> bool:
        # never open a position in a market that has already resolved
        # (bootstrap/lagged events; also avoids the volume-proxy fallback
        # that closed markets get from market_liquidity)
        closed = market_is_closed(
            slug=ev.market or ev.event_slug or None, condition_id=ev.condition_id
        )
        if closed:
            return False
        liq = market_liquidity(
            slug=ev.market or ev.event_slug or None, condition_id=ev.condition_id
        )
        ok, _ = trade_ok(trade_notional=ev.notional, liquidity=liq, cfg=cfg)
        return ok

    def domain_of(ev) -> str:
        return _domain_key({"event_slug": ev.event_slug, "slug": ev.market})

    summary = ingest_events(
        ledger,
        events,
        alloc=alloc,
        cfg=cfg,
        liquidity_ok=liq_ok,
        leader_values=leader_values,
        domain_fn=domain_of,
    )
    settled = settle_resolved(ledger, is_closed_fn=market_is_closed, price_fn=market_mark_price)
    evicted = evict_drawdown_wallets(
        ledger,
        max_dd=float(risk.get("wallet_evict_drawdown", 0.20)),
        price_fn=market_mark_price,
    )
    for rec in evicted:
        evict_member(rec["wallet"], rec["reason"])
        alloc.pop(rec["wallet"], None)
    pv = mark_positions(ledger, price_fn=market_mark_price)
    equity = update_equity_and_halt(
        ledger,
        pv,
        halt_drawdown=float(risk.get("portfolio_halt_drawdown", 0.15)),
        halt_enabled=bool(risk.get("halt_enabled", False)),
    )
    points = load_equity()
    append_equity_point(
        points,
        ts=datetime.now(timezone.utc).isoformat(),
        equity=equity,
        cash=float(ledger["cash"]),
        positions_value=pv,
        n_open=len(ledger["positions"]),
        realized_pnl_cum=float(ledger["realized_pnl_cum"]),
        max_points=int(cfg.get("ledger", {}).get("equity_max_points", 2000)),
    )
    save_equity(points)
    save_ledger(ledger)
    summary.update(
        {
            "settled": settled,
            "evicted": [{"wallet": e["wallet"], "reason": e["reason"]} for e in evicted],
            "equity": round(equity, 4),
            "cash": round(float(ledger["cash"]), 4),
            "positions_value": round(pv, 4),
            "n_open": len(ledger["positions"]),
            "realized_pnl_cum": round(float(ledger["realized_pnl_cum"]), 4),
            "halted": ledger["halted"],
            "halt_reason": ledger["halt_reason"],
            "would_halt": ledger.get("would_halt", False),
            "would_halt_reason": ledger.get("would_halt_reason", ""),
        }
    )
    return summary


def cmd_paper(args: argparse.Namespace) -> int:
    """Incremental paper follow: only fills events newer than the ledger cursor."""
    from poly_copy.ledger import load_ledger

    cfg = load_config(args.config)
    if args.mode:
        cfg.setdefault("copy", {})["mode"] = args.mode
    if getattr(args, "live_liq", False):
        cfg.setdefault("copy", {})["require_liquidity"] = True
    store = default_store(cfg)
    wallets = _wallets_from_args(args, cfg)
    snaps = _load_snaps(wallets, store, cfg, args.refresh)
    features = [compute_features(s) for s in snaps]
    ranked = rank_universe(features, cfg)
    alloc: Allocation = allocate(ranked, cfg)
    from poly_copy.universe import load_universe

    uni = load_universe()
    if uni.get("allocation") and not args.wallet and not args.wallets:
        # follow system portfolio weights when running the maintained universe
        alloc = {str(k).lower(): float(v) for k, v in uni["allocation"].items()}
        cfg.setdefault("copy", {})["mode"] = args.mode or "portfolio"
    if not alloc:
        alloc = {s.address: 1.0 / len(snaps) for s in snaps}

    events = []
    for snap in snaps:
        events.extend(snap.events())
    events.sort(key=lambda e: e.timestamp or 0)

    ledger = load_ledger()
    _bootstrap_cursors(ledger, [s.address for s in snaps], cfg)
    leader_values = _fetch_leader_values([s.address for s in snaps])
    summary = _ledger_cycle(ledger, events, alloc, cfg, leader_values=leader_values)
    fills = summary.pop("fills", [])
    _print(
        {
            "allocation": alloc,
            **summary,
            "fills": fills[: args.limit],
            "fills_truncated": len(fills) > args.limit,
        }
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = default_store(cfg)
    wallets = _wallets_from_args(args, cfg)
    snaps = _load_snaps(wallets, store, cfg, args.refresh)
    features = [compute_features(s) for s in snaps]
    ranked = rank_universe(features, cfg)
    alloc = allocate(ranked, cfg)
    if not alloc:
        alloc = {s.address: 1.0 / len(snaps) for s in snaps}

    events = []
    for snap in snaps:
        events.extend(snap.events())
    events.sort(key=lambda e: e.timestamp or 0)
    bt = run_backtest(events, alloc, cfg)
    dispersion = domain_dispersion(features, alloc)

    drift_notes = []
    if len(features) >= 1 and args.baseline_cache:
        base_path = Path(args.baseline_cache)
        if base_path.exists():
            from poly_copy.data import WalletSnapshot

            with base_path.open(encoding="utf-8") as f:
                base_snap = WalletSnapshot.from_dict(json.load(f))
            base_feat = compute_features(base_snap)
            for feat in features:
                if feat.address == base_feat.address:
                    drift_notes.append(detect_drift(base_feat, feat, cfg).to_dict())

    _print(
        {
            "allocation": alloc,
            "domain_dispersion": dispersion,
            "scores": [s.to_dict() for _, s in ranked],
            "backtest": bt.to_dict(),
            "max_drawdown": bt.max_drawdown,
            "simulated_equity": bt.equity_curve[-1] if bt.equity_curve else None,
            "drift": drift_notes,
        }
    )
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    if args.grid:
        from poly_copy.backtest.grid import run_grid

        cfg = load_config(args.config)
        result = run_grid(cfg, trade_limit=args.grid_limit)
        out = Path(args.out) if args.out else PACKAGE_ROOT / "dashboard" / "backtest_grid.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(str(out), file=sys.stderr)
        print(f"n_events={result.get('n_events')} n_combos={result.get('n_total')}", file=sys.stderr)
        print(f"positive_combos={result.get('n_positive')}/{result.get('n_total')}", file=sys.stderr)
        print(f"best={result.get('best')}", file=sys.stderr)
        print(f"by_delay_seconds={result.get('by_delay_seconds')}", file=sys.stderr)
        _print(result)
        return 0

    cfg = load_config(args.config)
    store = default_store(cfg)
    wallets = _wallets_from_args(args, cfg)
    snaps = _load_snaps(wallets, store, cfg, args.refresh)
    features = [compute_features(s) for s in snaps]
    ranked = rank_universe(features, cfg)
    alloc = allocate(ranked, cfg) or {s.address: 1.0 / len(snaps) for s in snaps}
    events = []
    for snap in snaps:
        events.extend(snap.events())
    events.sort(key=lambda e: e.timestamp or 0)

    if args.scan:
        results = param_scan(events, alloc, cfg)
        _print({"scan": [r.to_dict() for r in results]})
    else:
        bt = run_backtest(events, alloc, cfg)
        _print(bt.to_dict())
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Guide-style wallet discovery from public leaderboard + hard filters."""
    from poly_copy.discover import discover_wallets

    cfg = load_config(args.config)
    if args.limit:
        cfg.setdefault("discover", {})["max_results"] = int(args.limit)
    if args.candidates:
        cfg.setdefault("discover", {})["max_candidates"] = int(args.candidates)
    result = discover_wallets(cfg)
    out = Path(args.out) if args.out else PACKAGE_ROOT / "dashboard" / "discover.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(str(out), file=sys.stderr)
    _print(result)
    return 0


def cmd_attribution(args: argparse.Namespace) -> int:
    """Read dashboard/ledger.json + equity.json, write dashboard/attribution.json,
    print a summary: PnL/slippage/turnover by source wallet, by domain, and by
    time-to-settlement bucket."""
    from poly_copy.attribution import build_attribution

    report = build_attribution(fetch_end_dates=not args.no_end_dates)
    out = Path(args.out) if args.out else PACKAGE_ROOT / "dashboard" / "attribution.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(str(out), file=sys.stderr)

    def top(d: dict[str, Any], key: str, n: int = 3) -> list[tuple[str, float]]:
        rows = [(k, v[key]) for k, v in d.items() if isinstance(v, dict) and key in v]
        return sorted(rows, key=lambda kv: kv[1])[:n]

    print(f"data_source={report['data_source']} n_fills={report['n_fills_logged']}", file=sys.stderr)
    print(f"total_pnl={report['total_pnl']}", file=sys.stderr)
    print(
        f"worst wallets: {top(report['by_source_wallet'], 'realized_pnl_cumulative')}",
        file=sys.stderr,
    )
    print(f"worst domains: {top(report['by_domain'], 'realized_pnl')}", file=sys.stderr)
    print(
        f"slippage_cost={report['slippage']['total_cost']} "
        f"share_of_loss={report['slippage'].get('share_of_total_loss')}",
        file=sys.stderr,
    )
    _print(report)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Write dashboard/data.json for the HTML status page."""
    from datetime import datetime, timezone

    from poly_copy.backtest import run_backtest
    from poly_copy.portfolio import allocate, domain_dispersion

    cfg = load_config(args.config)
    store = default_store(cfg)
    wallets = _wallets_from_args(args, cfg)
    snaps = _load_snaps(wallets, store, cfg, args.refresh)
    features = [compute_features(s) for s in snaps]
    ranked = rank_universe(features, cfg)
    alloc = allocate(ranked, cfg) or {s.address: 1.0 / len(snaps) for s in snaps}
    events = []
    for snap in snaps:
        events.extend(snap.events())
    events.sort(key=lambda e: e.timestamp or 0)
    bt = run_backtest(events, alloc, cfg)
    primary = snaps[0]
    feat = features[0]
    sc = ranked[0][1]
    out_dir = Path(args.out) if args.out else PACKAGE_ROOT / "dashboard"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "paper",
        "repo": "Anson-gzy/poly-copy",
        "wallet": {
            "address": primary.address,
            "profile": primary.profile,
            "fetched_at": primary.fetched_at,
            "leaderboard_pnl": primary.leaderboard_pnl,
            "leaderboard_vol": primary.leaderboard_vol,
            "portfolio_value": primary.portfolio_value,
            "traded_market_count": primary.traded_market_count,
            "trade_count": len(primary.trades),
            "open_positions": len(primary.positions),
            "closed_positions": len(primary.closed_positions),
        },
        "features": feat.to_dict(),
        "score": sc.to_dict(),
        "verdict": "适合跟" if sc.suitable else f"不适合跟 ({sc.hard_reject_reason})",
        "allocation": alloc,
        "domain_dispersion": domain_dispersion(features, alloc),
        "backtest": bt.to_dict(),
        "recent_trades": sorted(
            primary.trades, key=lambda t: t.get("timestamp") or "", reverse=True
        )[:40],
    }
    path = out_dir / "data.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(str(path), file=sys.stderr)
    _print({"ok": True, "path": str(path), "verdict": payload["verdict"]})
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = default_store(cfg)
    wallets = _wallets_from_args(args, cfg)
    for w in wallets:
        snap = fetch_wallet(w, cfg=cfg)
        path = store.save(snap)
        print(
            f"{snap.address} trades={len(snap.trades)} positions={len(snap.positions)} "
            f"closed={len(snap.closed_positions)} pnl={snap.leaderboard_pnl} -> {path}",
            file=sys.stderr,
        )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Fast poll: only trades newer than the persistent ledger cursor."""
    import time
    import urllib.parse
    import urllib.request
    from datetime import datetime, timezone

    from poly_copy.ledger import load_ledger
    from poly_copy.types import WalletEvent

    cfg = load_config(args.config)
    cfg.setdefault("copy", {})["require_liquidity"] = True
    if args.mode:
        cfg["copy"]["mode"] = args.mode
    wallets = _wallets_from_args(args, cfg)
    from poly_copy.universe import load_universe

    uni = load_universe()
    if uni.get("allocation") and not args.wallet and not args.wallets:
        alloc = {str(k).lower(): float(v) for k, v in uni["allocation"].items()}
        cfg.setdefault("copy", {})["mode"] = args.mode or "portfolio"
    else:
        alloc = {w: 1.0 / len(wallets) for w in wallets}
    interval = float(args.interval or cfg.get("copy", {}).get("poll_seconds", 15))
    limit = int(cfg.get("copy", {}).get("poll_trade_limit", 20))
    print(f"watch wallets={len(wallets)} interval={interval}s liquidity_gate=on", file=sys.stderr)

    while True:
        ledger = load_ledger()
        _bootstrap_cursors(ledger, wallets, cfg)
        events: list[WalletEvent] = []
        for w in wallets:
            url = f"https://data-api.polymarket.com/trades?{urllib.parse.urlencode({'user': w, 'limit': limit})}"
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(
                        url,
                        headers={
                            "accept": "application/json",
                            "user-agent": "Mozilla/5.0 (compatible; poly-copy/0.1)",
                        },
                    ),
                    timeout=10,
                ) as resp:
                    trades = json.loads(resp.read().decode())
            except Exception as e:
                print(f"poll_error {w}: {e}", file=sys.stderr)
                continue
            if not isinstance(trades, list):
                continue
            for t in trades:
                ts = float(t.get("timestamp") or 0)
                size = float(t.get("size") or 0)
                price = float(t.get("price") or 0)
                events.append(
                    WalletEvent(
                        address=w,
                        side=str(t.get("side") or "BUY").upper(),
                        size=size,
                        price=price,
                        notional=size * price,
                        market=str(t.get("slug") or t.get("title") or ""),
                        event_slug=str(t.get("eventSlug") or ""),
                        outcome=str(t.get("outcome") or ""),
                        timestamp=(
                            datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else None
                        ),
                        tx_hash=t.get("transactionHash"),
                        condition_id=t.get("conditionId"),
                        token_id=str(t.get("asset") or "") or None,
                    )
                )
        leader_values = _fetch_leader_values(wallets)
        summary = _ledger_cycle(ledger, events, alloc, cfg, leader_values=leader_values)
        print(
            f"watch cycle: polled={summary['n_events']} new={summary['n_new']} "
            f"buys={summary['n_buys']} sells={summary['n_sells']} "
            f"equity={summary['equity']} halted={summary['halted']}",
            file=sys.stderr,
        )
        _print(summary)
        if args.once:
            return 0
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="poly-copy", description="Polymarket wallet copy framework (paper)")
    p.add_argument("--config", default=str(PACKAGE_ROOT / "configs" / "default.yaml"))
    p.add_argument("--refresh", action="store_true", help="Bypass cache and refetch")
    sub = p.add_subparsers(dest="command", required=True)

    def add_wallet_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--wallet", help="Single wallet address")
        sp.add_argument("--wallets", nargs="+", help="Multiple wallet addresses")
        sp.add_argument(
            "--universe",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Use dashboard/universe.json members (default: auto if file has members)",
        )

    uni = sub.add_parser("universe", help="Maintain 10 suitable wallets + allocation")
    uni.add_argument("action", choices=["sync", "show"], help="sync=validate+refill; show=print state")
    uni.add_argument("--no-refresh", action="store_true", help="Reuse cache when re-checking members")
    uni.set_defaults(func=cmd_universe)

    for name, help_, fn in [
        ("fetch", "Fetch and cache wallet public data", cmd_fetch),
        ("screen", "Features + score + suitable verdict", cmd_screen),
        ("score", "Rank universe and allocation", cmd_score),
        ("paper", "Paper copy log from cached/public trades", cmd_paper),
        ("report", "Portfolio weights, dispersion, simulated equity", cmd_report),
        ("backtest", "Historical replay / param scan", cmd_backtest),
        ("watch", "Fast poll newest trades with liquidity gate", cmd_watch),
        ("dashboard", "Export dashboard/data.json for the HTML page", cmd_dashboard),
        ("discover", "Find wallets via leaderboard + guide hard filters", cmd_discover),
        ("attribution", "PnL/slippage/turnover attribution by wallet/domain/time", cmd_attribution),
    ]:
        sp = sub.add_parser(name, help=help_)
        add_wallet_args(sp)
        sp.set_defaults(func=fn)
        if name == "paper":
            sp.add_argument("--mode", choices=["fixed", "portfolio"])
            sp.add_argument("--limit", type=int, default=50, help="Max fills to print")
            sp.add_argument(
                "--live-liq",
                action="store_true",
                help="Gate historical paper fills by current market liquidity",
            )
        if name == "report":
            sp.add_argument("--baseline-cache", help="Prior snapshot JSON for drift check")
        if name == "backtest":
            sp.add_argument("--scan", action="store_true", help="Scan fixed notional × stop loss")
            sp.add_argument(
                "--grid",
                action="store_true",
                help="Replay real universe/ledger trade history across a param grid "
                "(fixed_notional × stop_loss × settlement filter × delay); writes "
                "dashboard/backtest_grid.json",
            )
            sp.add_argument(
                "--grid-limit", type=int, default=300, help="Trades to re-pull per wallet for --grid"
            )
            sp.add_argument("--out", help="Output JSON path for --grid (default: dashboard/backtest_grid.json)")
        if name == "watch":
            sp.add_argument("--mode", choices=["fixed", "portfolio"])
            sp.add_argument("--interval", type=float, help="Poll seconds (default 15)")
            sp.add_argument("--once", action="store_true", help="Single poll then exit")
        if name == "dashboard":
            sp.add_argument("--out", help="Output directory (default: dashboard/)")
        if name == "discover":
            sp.add_argument("--limit", type=int, help="Max passed wallets to return")
            sp.add_argument("--candidates", type=int, help="Max leaderboard candidates to probe")
            sp.add_argument("--out", help="Write JSON path (default: dashboard/discover.json)")
        if name == "attribution":
            sp.add_argument("--out", help="Write JSON path (default: dashboard/attribution.json)")
            sp.add_argument(
                "--no-end-dates",
                action="store_true",
                help="Skip Gamma endDate lookups (faster, no time-to-settlement buckets)",
            )
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
