"""Maintain ~10 suitable wallets: validate, refill via discover, allocate weights."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from poly_copy.config import PACKAGE_ROOT
from poly_copy.discover import (
    DiscoverCandidate,
    _exit_reject,
    _probe,
    discover_wallets,
)
from poly_copy.portfolio import allocate
from poly_copy.types import Allocation, WalletFeatures, WalletScore


@dataclass
class UniverseMember:
    address: str
    score: float
    suitable: bool
    reject_reason: str | None = None
    user_name: str | None = None
    pnl: float | None = None
    weight: float = 0.0
    tags: list[str] = field(default_factory=list)
    checked_at: str = ""
    exit_strikes: int = 0  # consecutive exit-screen failures

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_universe_path() -> Path:
    return PACKAGE_ROOT / "dashboard" / "universe.json"


def load_universe(path: Path | None = None) -> dict[str, Any]:
    p = path or default_universe_path()
    if not p.exists():
        return {"updated_at": None, "target_n": 10, "members": [], "allocation": {}, "dropped": []}
    return json.loads(p.read_text(encoding="utf-8"))


def save_universe(state: dict[str, Any], path: Path | None = None) -> Path:
    p = path or default_universe_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p


def _guide_score(c: DiscoverCandidate) -> float:
    """Rank 'best' among guide-pass wallets without full feature pipeline."""
    wr = max(0.0, min(1.0, c.win_rate))
    pnl_n = max(0.0, min(1.0, (c.pnl - 15000) / (400000 - 15000)))
    pos_n = max(0.0, min(1.0, c.position_value / 100000))
    act_n = max(0.0, min(1.0, c.active_markets / 20))
    return 0.35 * wr + 0.35 * pnl_n + 0.2 * pos_n + 0.1 * act_n


def _probe_candidate(
    address: str,
    *,
    pnl: float = 0.0,
    vol: float = 0.0,
    user_name: str | None = None,
    rank: str | None = None,
    source_period: str = "ACTIVE",
) -> DiscoverCandidate:
    stats = _probe(address)
    # if probe lacks pnl (re-check path), keep provided pnl
    return DiscoverCandidate(
        address=address.lower(),
        user_name=user_name,
        pnl=float(pnl or 0),
        vol=float(vol or 0),
        rank=rank,
        source_period=source_period,
        position_value=float(stats["position_value"]),
        active_markets=int(stats["active_markets"]),
        trade_count=int(stats["trade_count"]),
        traded_markets=int(stats["traded_markets"]),
        win_rate=float(stats["win_rate"]),
        closed_sample=int(stats["closed_sample"]),
    )


def _as_scored_pair(c: DiscoverCandidate) -> tuple[WalletFeatures, WalletScore]:
    """Minimal WalletFeatures/Score so allocate() can run."""
    feat = WalletFeatures(
        address=c.address,
        sample_days=60.0,
        trade_count=max(c.trade_count, c.traded_markets),
        monthly_freq=40.0,
        win_rate=c.win_rate,
        profit_factor=1.5,
        max_drawdown=0.1,
        focus_score=0.7,
        stability_score=0.7,
        position_value=c.position_value,
        realized_pnl=c.pnl,
        unrealized_pnl=0.0,
        total_pnl=c.pnl,
        active_markets=c.active_markets,
        top_domains=["mixed"],
        liquid_trade_share=0.8,
        median_market_liquidity=20000,
    )
    sc = WalletScore(
        address=c.address,
        score=_guide_score(c),
        hard_reject_reason=None,
        tags=["guide_pass"],
        components={"guide": _guide_score(c)},
    )
    return feat, sc


def _discover_pool(
    cfg: dict[str, Any],
    *,
    need: int,
    exclude: set[str],
) -> list[DiscoverCandidate]:
    dcfg = dict(cfg.get("discover", {}))
    target = max(need * 3, need + 8)
    candidates = int(dcfg.get("max_candidates", 80))
    lb_limit = int(dcfg.get("leaderboard_limit", 150))
    collected: dict[str, DiscoverCandidate] = {}
    for _ in range(5):
        trial_cfg = dict(cfg)
        trial_cfg["discover"] = {
            **dcfg,
            "max_candidates": candidates,
            "leaderboard_limit": lb_limit,
            "max_results": max(target * 2, 30),
        }
        result = discover_wallets(trial_cfg, exclude=exclude | set(collected))
        for row in result.get("results", []):
            addr = str(row["address"]).lower()
            if addr in exclude or addr in collected:
                continue
            fields = {k: row[k] for k in row if k in DiscoverCandidate.__dataclass_fields__}
            collected[addr] = DiscoverCandidate(**fields)
        if len(collected) >= target:
            break
        candidates = min(candidates + 50, 250)
        lb_limit = min(lb_limit + 100, 400)
    return list(collected.values())


def sync_universe(
    cfg: dict[str, Any],
    *,
    refresh: bool = True,
    path: Path | None = None,
) -> dict[str, Any]:
    """
    Re-check members with exit_screen (hysteresis); drop only after strikes_required fails.
    If < target_n, rediscover with hard_screen entry filters and pick best; allocate weights.
    """
    _ = refresh  # probes always hit live APIs
    pc = cfg.get("portfolio", {})
    target_n = int(pc.get("n_wallets", 10))
    exit_cfg = cfg.get("exit_screen") or {}
    strikes_required = int(exit_cfg.get("strikes_required", 2))
    prev = load_universe(path)
    now = datetime.now(timezone.utc).isoformat()

    kept: list[UniverseMember] = []
    dropped: list[dict[str, Any]] = []
    scored_pairs: list[tuple[WalletFeatures, WalletScore]] = []
    warned: list[dict[str, Any]] = []

    for m in prev.get("members") or []:
        addr = str(m.get("address") or "").lower()
        if not addr:
            continue
        prev_strikes = int(m.get("exit_strikes") or 0)
        cand = _probe_candidate(
            addr,
            pnl=float(m.get("pnl") or 0),
            user_name=m.get("user_name"),
        )
        reason = _exit_reject(cand, cfg)
        if reason and reason.startswith("pnl_below") and float(m.get("pnl") or 0) >= float(
            exit_cfg.get("pnl_min", cfg.get("hard_screen", {}).get("pnl_min", 5000))
        ):
            cand.pnl = float(m["pnl"])
            reason = _exit_reject(cand, cfg)

        score = _guide_score(cand)
        if reason is None:
            strikes = 0
            member = UniverseMember(
                address=addr,
                score=score,
                suitable=True,
                reject_reason=None,
                user_name=cand.user_name or m.get("user_name"),
                pnl=cand.pnl or m.get("pnl"),
                tags=["active"],
                checked_at=now,
                exit_strikes=0,
            )
            kept.append(member)
            scored_pairs.append(_as_scored_pair(cand))
        else:
            strikes = prev_strikes + 1
            member = UniverseMember(
                address=addr,
                score=score,
                suitable=False,
                reject_reason=reason,
                user_name=cand.user_name or m.get("user_name"),
                pnl=cand.pnl or m.get("pnl"),
                tags=["exit_warn"] if strikes < strikes_required else ["exited"],
                checked_at=now,
                exit_strikes=strikes,
            )
            if strikes >= strikes_required:
                dropped.append(member.to_dict())
            else:
                # keep with warning — do not churn yet
                member.suitable = True
                member.tags = ["exit_warn", f"strikes:{strikes}/{strikes_required}"]
                kept.append(member)
                scored_pairs.append(_as_scored_pair(cand))
                warned.append(member.to_dict())

    added: list[UniverseMember] = []
    if len(kept) < target_n:
        need = target_n - len(kept)
        exclude = {m.address for m in kept} | {d["address"] for d in dropped}
        pool = _discover_pool(cfg, need=need, exclude=exclude)
        pool.sort(key=_guide_score, reverse=True)
        for cand in pool:
            if len(kept) >= target_n:
                break
            if cand.address in exclude:
                continue
            member = UniverseMember(
                address=cand.address,
                score=_guide_score(cand),
                suitable=True,
                reject_reason=None,
                user_name=cand.user_name,
                pnl=cand.pnl,
                tags=["guide_pass", "refilled"],
                checked_at=now,
            )
            kept.append(member)
            added.append(member)
            scored_pairs.append(_as_scored_pair(cand))
            exclude.add(cand.address)

    kept.sort(key=lambda m: m.score, reverse=True)
    kept = kept[:target_n]
    scored_pairs = scored_pairs[:target_n]

    alloc: Allocation = allocate(scored_pairs, cfg)
    if not alloc and kept:
        alloc = {m.address: 1.0 / len(kept) for m in kept}
    for m in kept:
        m.weight = float(alloc.get(m.address, 0.0))

    state = {
        "updated_at": now,
        "target_n": target_n,
        "active_n": len(kept),
        "shortfall": max(0, target_n - len(kept)),
        "members": [m.to_dict() for m in kept],
        "allocation": alloc,
        "dropped": dropped[-20:],
        "warned": warned[-20:],
        "added": [m.to_dict() for m in added],
        "exit_policy": {
            "strikes_required": strikes_required,
            "screen": "exit_screen",
            "entry_screen": "hard_screen",
        },
        "status": "ok" if len(kept) >= target_n else "short",
    }
    save_universe(state, path)
    return state


def universe_wallets(path: Path | None = None) -> list[str]:
    state = load_universe(path)
    return [str(m["address"]).lower() for m in state.get("members") or [] if m.get("address")]
