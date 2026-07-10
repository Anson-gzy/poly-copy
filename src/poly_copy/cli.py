"""CLI: poly-copy screen|score|paper|report|backtest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from poly_copy.backtest import param_scan, run_backtest
from poly_copy.config import PACKAGE_ROOT, load_config
from poly_copy.copy import paper_copy_events
from poly_copy.data import WalletStore, default_store, fetch_wallet
from poly_copy.features import compute_features
from poly_copy.portfolio import allocate, domain_dispersion
from poly_copy.risk import RiskGuard, detect_drift
from poly_copy.score import rank_universe, score_wallet
from poly_copy.types import Allocation, RiskAction


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _wallets_from_args(args: argparse.Namespace, cfg: dict[str, Any]) -> list[str]:
    if getattr(args, "wallets", None):
        return [w.strip().lower() for w in args.wallets if w.strip()]
    if getattr(args, "wallet", None):
        return [args.wallet.strip().lower()]
    return [str(cfg.get("case_wallet", "")).lower()]


def _load_snaps(
    wallets: list[str],
    store: WalletStore,
    cfg: dict[str, Any],
    refresh: bool,
):
    snaps = []
    for w in wallets:
        snaps.append(store.get_or_fetch(w, refresh=refresh, cfg=cfg))
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


def cmd_paper(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.mode:
        cfg.setdefault("copy", {})["mode"] = args.mode
    store = default_store(cfg)
    wallets = _wallets_from_args(args, cfg)
    snaps = _load_snaps(wallets, store, cfg, args.refresh)
    features = [compute_features(s) for s in snaps]
    ranked = rank_universe(features, cfg)
    alloc: Allocation = allocate(ranked, cfg)
    if not alloc:
        # still paper-follow even if hard-rejected, equal weight for demo
        alloc = {s.address: 1.0 / len(snaps) for s in snaps}

    events = []
    for snap in snaps:
        events.extend(snap.events())
    events.sort(key=lambda e: e.timestamp or 0)

    capital = float(cfg.get("backtest", {}).get("initial_capital", 1000.0))
    guard = RiskGuard(cfg, initial_capital=capital)

    def risk_fn(intent, fills):
        return guard.check_intent(intent, fills)

    fills = paper_copy_events(events, alloc, cfg, risk_fn=risk_fn)
    for f in fills:
        # lightweight mark
        pnl = -f.slippage * f.fill_size
        if f.stopped:
            pnl = f.pnl
        else:
            f.pnl = pnl
        guard.on_fill(f, pnl)
        guard.mark_stop_on_fill(f)

    _print(
        {
            "allocation": alloc,
            "n_events": len(events),
            "n_fills": len(fills),
            "halted": guard.state.halted,
            "halt_reason": guard.state.halt_reason,
            "equity": guard.state.equity,
            "fills": [f.to_dict() for f in fills[: args.limit]],
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="poly-copy", description="Polymarket wallet copy framework (paper)")
    p.add_argument("--config", default=str(PACKAGE_ROOT / "configs" / "default.yaml"))
    p.add_argument("--refresh", action="store_true", help="Bypass cache and refetch")
    sub = p.add_subparsers(dest="command", required=True)

    def add_wallet_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--wallet", help="Single wallet address")
        sp.add_argument("--wallets", nargs="+", help="Multiple wallet addresses")

    for name, help_, fn in [
        ("fetch", "Fetch and cache wallet public data", cmd_fetch),
        ("screen", "Features + score + suitable verdict", cmd_screen),
        ("score", "Rank universe and allocation", cmd_score),
        ("paper", "Paper copy log from cached/public trades", cmd_paper),
        ("report", "Portfolio weights, dispersion, simulated equity", cmd_report),
        ("backtest", "Historical replay / param scan", cmd_backtest),
    ]:
        sp = sub.add_parser(name, help=help_)
        add_wallet_args(sp)
        sp.set_defaults(func=fn)
        if name == "paper":
            sp.add_argument("--mode", choices=["fixed", "portfolio"])
            sp.add_argument("--limit", type=int, default=50, help="Max fills to print")
        if name == "report":
            sp.add_argument("--baseline-cache", help="Prior snapshot JSON for drift check")
        if name == "backtest":
            sp.add_argument("--scan", action="store_true", help="Scan fixed notional × stop loss")

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
