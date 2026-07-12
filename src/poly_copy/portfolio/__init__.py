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


def cap_domain_weights(
    allocation: Allocation,
    domain_by_addr: dict[str, str],
    *,
    cap: float = 0.40,
    max_iter: int = 5,
) -> Allocation:
    """Cap any single domain's combined weight at `cap` (default 40%).

    Overweight domains are scaled down proportionally and the surplus is
    redistributed to wallets in other domains pro-rata, then normalized.
    Wallets with no known domain get their own singleton bucket. If every
    wallet shares one domain the cap cannot be satisfied; the allocation is
    returned normalized as-is.
    """
    if not allocation:
        return {}
    alloc = {a: max(0.0, float(w)) for a, w in allocation.items()}
    total = sum(alloc.values())
    if total <= 0:
        return {a: 1.0 / len(alloc) for a in alloc}
    alloc = {a: w / total for a, w in alloc.items()}

    def dom(addr: str) -> str:
        return domain_by_addr.get(addr) or f"__solo__:{addr}"

    domains = {dom(a) for a in alloc}
    if len(domains) <= 1:
        return alloc

    for _ in range(max_iter):
        by_dom: dict[str, float] = defaultdict(float)
        for a, w in alloc.items():
            by_dom[dom(a)] += w
        over = {d: w for d, w in by_dom.items() if w > cap + 1e-9}
        if not over:
            break
        surplus = 0.0
        for d, w in over.items():
            scale = cap / w
            for a in alloc:
                if dom(a) == d:
                    surplus += alloc[a] * (1 - scale)
                    alloc[a] *= scale
        receivers = {a: w for a, w in alloc.items() if dom(a) not in over}
        recv_total = sum(receivers.values())
        if recv_total <= 0:
            break
        for a in receivers:
            alloc[a] += surplus * (alloc[a] / recv_total)

    total = sum(alloc.values())
    return {a: w / total for a, w in alloc.items()} if total > 0 else alloc


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
