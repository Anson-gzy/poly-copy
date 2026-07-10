"""Hard screen, blacklist heuristics, reliability score."""

from __future__ import annotations

from typing import Any

from poly_copy.types import WalletFeatures, WalletScore


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def hard_screen(feat: WalletFeatures, cfg: dict[str, Any]) -> str | None:
    hs = cfg.get("hard_screen", {})
    if feat.total_pnl < float(hs.get("pnl_min", 15000)):
        return f"pnl_below_min:{feat.total_pnl:.0f}"
    if feat.total_pnl > float(hs.get("pnl_max", 400000)):
        return f"pnl_above_max:{feat.total_pnl:.0f}"
    if feat.position_value < float(hs.get("position_value_min", 5000)):
        return f"position_value_low:{feat.position_value:.0f}"
    if feat.active_markets < int(hs.get("active_markets_min", 2)):
        return f"active_markets_low:{feat.active_markets}"
    if feat.trade_count < int(hs.get("trades_min", 20)):
        return f"trades_low:{feat.trade_count}"
    if feat.win_rate < float(hs.get("win_rate_min", 0.70)):
        return f"win_rate_low:{feat.win_rate:.2f}"
    return None


def blacklist(feat: WalletFeatures, cfg: dict[str, Any]) -> str | None:
    bl = cfg.get("blacklist", {})
    if feat.monthly_freq > float(bl.get("monthly_freq_max", 400)):
        return f"hft_freq:{feat.monthly_freq:.0f}"
    if feat.single_event_pnl_share > float(bl.get("single_event_pnl_share_max", 0.80)):
        return f"single_event_rich:{feat.single_event_pnl_share:.2f}"
    if feat.avg_position_value < float(bl.get("avg_position_value_min", 50)) and feat.trade_count > 0:
        # only flag when we have open positions to judge
        if feat.meta.get("open_count", 0) > 0:
            return f"low_liquidity_avg_pos:{feat.avg_position_value:.1f}"
    return None


def _component_scores(feat: WalletFeatures, cfg: dict[str, Any]) -> dict[str, float]:
    bl = cfg.get("blacklist", {})
    pref_lo = float(bl.get("monthly_freq_pref_min", 30))
    pref_hi = float(bl.get("monthly_freq_pref_max", 200))
    if pref_lo <= feat.monthly_freq <= pref_hi:
        freq_bonus = 1.0
    else:
        mid = (pref_lo + pref_hi) / 2
        freq_bonus = _clip01(1.0 - abs(feat.monthly_freq - mid) / mid)

    return {
        "stability": _clip01(feat.stability_score) * (0.7 + 0.3 * freq_bonus),
        "drawdown_control": _clip01(1.0 - feat.max_drawdown),
        "focus": _clip01(feat.focus_score),
        "profit_factor": _clip01(feat.profit_factor / 3.0),  # PF 3 → full
        "win_rate": _clip01(feat.win_rate),
    }


def score_wallet(feat: WalletFeatures, cfg: dict[str, Any]) -> WalletScore:
    tags: list[str] = []
    reject = hard_screen(feat, cfg)
    if reject:
        tags.append("hard_reject")
    else:
        bl = blacklist(feat, cfg)
        if bl:
            reject = bl
            tags.append("blacklist")

    comps = _component_scores(feat, cfg)
    weights = cfg.get("score_weights", {})
    score = 0.0
    wsum = 0.0
    for k, v in comps.items():
        w = float(weights.get(k, 0.0))
        score += w * v
        wsum += w
    if wsum > 0:
        score /= wsum

    if reject is None:
        tags.append("suitable")
        if feat.focus_score >= 0.7:
            tags.append("focused")
        if feat.stability_score >= 0.6:
            tags.append("stable")
    else:
        tags.append("unsuitable")

    return WalletScore(
        address=feat.address,
        score=float(score) if reject is None else 0.0,
        hard_reject_reason=reject,
        tags=tags,
        components=comps,
    )


def rank_universe(
    features: list[WalletFeatures], cfg: dict[str, Any]
) -> list[tuple[WalletFeatures, WalletScore]]:
    ranked = [(f, score_wallet(f, cfg)) for f in features]
    ranked.sort(key=lambda x: (x[1].suitable, x[1].score), reverse=True)
    return ranked
