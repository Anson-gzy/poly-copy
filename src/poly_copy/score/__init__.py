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
    # guide: monthly trade frequency must sit in the 30–200 band (hard)
    freq_lo = float(hs.get("monthly_freq_min", 30))
    freq_hi = float(hs.get("monthly_freq_max", 200))
    if not (freq_lo <= feat.monthly_freq <= freq_hi):
        return f"monthly_freq_out_of_band:{feat.monthly_freq:.0f}"
    return None


def blacklist(feat: WalletFeatures, cfg: dict[str, Any]) -> str | None:
    bl = cfg.get("blacklist", {})
    liq = cfg.get("liquidity", {})
    if feat.monthly_freq > float(bl.get("monthly_freq_max", 200)):
        return f"hft_freq:{feat.monthly_freq:.0f}"
    # minute-level burst firing (bot signal) — from features meta
    burst = int(feat.meta.get("burst_max_per_minute", 0) or 0)
    if burst > int(bl.get("burst_per_minute_max", 8)):
        return f"hft_burst:{burst}/min"
    if feat.single_event_pnl_share > float(bl.get("single_event_pnl_share_max", 0.50)):
        return f"single_event_rich:{feat.single_event_pnl_share:.2f}"
    if feat.avg_position_value < float(bl.get("avg_position_value_min", 50)) and feat.trade_count > 0:
        # only flag when we have open positions to judge
        if feat.meta.get("open_count", 0) > 0:
            return f"low_liquidity_avg_pos:{feat.avg_position_value:.1f}"
    # prefer wallets that trade deep books, not one-sided thin markets
    if feat.meta.get("liquidity_markets_known", 0) >= 5:
        min_share = float(liq.get("min_liquid_trade_share", 0.50))
        if feat.liquid_trade_share < min_share:
            return f"thin_markets:{feat.liquid_trade_share:.2f}"
        min_med = float(liq.get("min_median_liquidity", 8_000))
        if feat.median_market_liquidity < min_med:
            return f"median_liq_low:{feat.median_market_liquidity:.0f}"
        max_dom = float(liq.get("max_wallet_trade_liq_share", 0.35))
        if feat.max_trade_liquidity_share > max_dom:
            return f"dominates_book:{feat.max_trade_liquidity_share:.2f}"
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
        "liquidity": _clip01(feat.liquid_trade_share) * _clip01(feat.median_market_liquidity / 50_000),
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
