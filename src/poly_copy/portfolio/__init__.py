"""Multi-wallet allocation and rebalance rules."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from poly_copy.types import Allocation, WalletFeatures, WalletScore


def allocate(
    scored: list[tuple[WalletFeatures, WalletScore]],
    cfg: dict[str, Any],
) -> Allocation:
    """Build target weights from suitable wallets."""
    pc = cfg.get("portfolio", {})
    n = int(pc.get("n_wallets", 10))
    method = str(pc.get("method", "score_weighted"))
    penalty = float(pc.get("correlation_penalty", 0.15))

    suitable = [(f, s) for f, s in scored if s.suitable]
    suitable = suitable[:n]
    if not suitable:
        return {}

    # domain overlap penalty: wallets sharing top domains get reduced raw weight
    domain_owners: dict[str, list[str]] = defaultdict(list)
    for f, _ in suitable:
        for d in f.top_domains[:2]:
            domain_owners[d].append(f.address)

    raw: dict[str, float] = {}
    for f, s in suitable:
        if method == "equal":
            w = 1.0
        else:
            w = max(s.score, 1e-6)
        overlap = 0
        for d in f.top_domains[:2]:
            overlap += max(0, len(domain_owners[d]) - 1)
        w *= max(0.2, 1.0 - penalty * overlap)
        raw[f.address] = w

    total = sum(raw.values())
    if total <= 0:
        return {a: 1.0 / len(raw) for a in raw}
    return {a: v / total for a, v in raw.items()}


def needs_rebalance(
    last_rebalance: datetime | None,
    cfg: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    days = int(cfg.get("portfolio", {}).get("rebalance_days", 7))
    now = now or datetime.now(timezone.utc)
    if last_rebalance is None:
        return True
    if last_rebalance.tzinfo is None:
        last_rebalance = last_rebalance.replace(tzinfo=timezone.utc)
    return now - last_rebalance >= timedelta(days=days)


def domain_dispersion(features: list[WalletFeatures], allocation: Allocation) -> dict[str, float]:
    """Weighted domain exposure for reporting."""
    by_addr = {f.address: f for f in features}
    exposure: dict[str, float] = defaultdict(float)
    for addr, w in allocation.items():
        f = by_addr.get(addr)
        if not f or not f.top_domains:
            exposure["unknown"] += w
            continue
        share = w / len(f.top_domains)
        for d in f.top_domains:
            exposure[d] += share
    return dict(exposure)
