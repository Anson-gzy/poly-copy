"""Wallet public data fetch + disk cache."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from poly_copy.config import load_config, resolve_cache_dir
from poly_copy.types import WalletEvent


def _dec(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class WalletSnapshot:
    address: str
    fetched_at: str
    trades: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    closed_positions: list[dict[str, Any]] = field(default_factory=list)
    activity: list[dict[str, Any]] = field(default_factory=list)
    portfolio_value: float = 0.0
    traded_market_count: int = 0
    leaderboard_pnl: float | None = None
    leaderboard_vol: float | None = None
    profile: dict[str, Any] | None = None

    def events(self) -> list[WalletEvent]:
        out: list[WalletEvent] = []
        for t in self.trades:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            out.append(
                WalletEvent(
                    address=self.address,
                    side=str(t.get("side") or "BUY").upper(),
                    size=size,
                    price=price,
                    notional=size * price,
                    market=str(t.get("slug") or t.get("title") or t.get("condition_id") or ""),
                    event_slug=str(t.get("event_slug") or ""),
                    outcome=str(t.get("outcome") or ""),
                    timestamp=_parse_iso(t.get("timestamp")),
                    tx_hash=t.get("transaction_hash"),
                    condition_id=t.get("condition_id"),
                    token_id=t.get("token_id"),
                )
            )
        out.sort(key=lambda e: e.timestamp or datetime.min.replace(tzinfo=timezone.utc))
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "fetched_at": self.fetched_at,
            "trades": self.trades,
            "positions": self.positions,
            "closed_positions": self.closed_positions,
            "activity": self.activity,
            "portfolio_value": self.portfolio_value,
            "traded_market_count": self.traded_market_count,
            "leaderboard_pnl": self.leaderboard_pnl,
            "leaderboard_vol": self.leaderboard_vol,
            "profile": self.profile,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WalletSnapshot:
        return cls(
            address=str(data["address"]),
            fetched_at=str(data.get("fetched_at") or ""),
            trades=list(data.get("trades") or []),
            positions=list(data.get("positions") or []),
            closed_positions=list(data.get("closed_positions") or []),
            activity=list(data.get("activity") or []),
            portfolio_value=float(data.get("portfolio_value") or 0),
            traded_market_count=int(data.get("traded_market_count") or 0),
            leaderboard_pnl=(
                float(data["leaderboard_pnl"]) if data.get("leaderboard_pnl") is not None else None
            ),
            leaderboard_vol=(
                float(data["leaderboard_vol"]) if data.get("leaderboard_vol") is not None else None
            ),
            profile=data.get("profile"),
        )


class WalletStore:
    """JSON cache for wallet snapshots."""

    def __init__(self, cache_dir: Path | str):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, address: str) -> Path:
        return self.cache_dir / f"{address.lower()}.json"

    def load(self, address: str) -> WalletSnapshot | None:
        path = self._path(address)
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as f:
            return WalletSnapshot.from_dict(json.load(f))

    def save(self, snap: WalletSnapshot) -> Path:
        path = self._path(snap.address)
        with path.open("w", encoding="utf-8") as f:
            json.dump(snap.to_dict(), f, ensure_ascii=False, indent=2, default=str)
        return path

    def get_or_fetch(
        self,
        address: str,
        *,
        refresh: bool = False,
        cfg: dict[str, Any] | None = None,
    ) -> WalletSnapshot:
        if not refresh:
            cached = self.load(address)
            if cached is not None:
                return cached
        snap = fetch_wallet(address, cfg=cfg)
        self.save(snap)
        return snap


def _take(items: Iterable[Any], limit: int) -> list[Any]:
    out: list[Any] = []
    for i, item in enumerate(items):
        if i >= limit:
            break
        out.append(item)
    return out


def _trade_row(t: Any) -> dict[str, Any]:
    return {
        "side": getattr(t, "side", None),
        "size": _dec(getattr(t, "size", None)),
        "price": _dec(getattr(t, "price", None)),
        "timestamp": _iso(getattr(t, "timestamp", None)),
        "title": getattr(t, "title", None),
        "slug": getattr(t, "slug", None),
        "event_slug": getattr(t, "event_slug", None),
        "outcome": getattr(t, "outcome", None),
        "condition_id": str(getattr(t, "condition_id", None) or "") or None,
        "token_id": str(getattr(t, "token_id", None) or "") or None,
        "transaction_hash": str(getattr(t, "transaction_hash", None) or "") or None,
    }


def _position_row(p: Any) -> dict[str, Any]:
    return {
        "condition_id": str(getattr(p, "condition_id", None) or "") or None,
        "size": _dec(getattr(p, "size", None)),
        "avg_price": _dec(getattr(p, "avg_price", None)),
        "current_value": _dec(getattr(p, "current_value", None)),
        "initial_value": _dec(getattr(p, "initial_value", None)),
        "cash_pnl": _dec(getattr(p, "cash_pnl", None)),
        "realized_pnl": _dec(getattr(p, "realized_pnl", None)),
        "percent_pnl": getattr(p, "percent_pnl", None),
        "title": getattr(p, "title", None),
        "slug": getattr(p, "slug", None),
        "event_slug": getattr(p, "event_slug", None),
        "outcome": getattr(p, "outcome", None),
        "cur_price": _dec(getattr(p, "cur_price", None)),
    }


def _closed_row(p: Any) -> dict[str, Any]:
    return {
        "condition_id": str(getattr(p, "condition_id", None) or "") or None,
        "avg_price": _dec(getattr(p, "avg_price", None)),
        "total_bought": _dec(getattr(p, "total_bought", None)),
        "realized_pnl": _dec(getattr(p, "realized_pnl", None)),
        "cur_price": _dec(getattr(p, "cur_price", None)),
        "timestamp": _iso(getattr(p, "timestamp", None)),
        "title": getattr(p, "title", None),
        "slug": getattr(p, "slug", None),
        "event_slug": getattr(p, "event_slug", None),
        "outcome": getattr(p, "outcome", None),
    }


def _activity_row(a: Any) -> dict[str, Any]:
    return {
        "type": getattr(a, "type", None),
        "side": getattr(a, "side", None),
        "amount": _dec(getattr(a, "amount", None)),
        "shares": _dec(getattr(a, "shares", None) or getattr(a, "size", None)),
        "price": _dec(getattr(a, "price", None)),
        "timestamp": _iso(getattr(a, "timestamp", None)),
        "title": getattr(a, "title", None),
        "slug": getattr(a, "slug", None),
        "event_slug": getattr(a, "event_slug", None),
        "outcome": getattr(a, "outcome", None),
        "transaction_hash": str(getattr(a, "transaction_hash", None) or "") or None,
    }


def fetch_wallet(address: str, cfg: dict[str, Any] | None = None) -> WalletSnapshot:
    """Pull public trades/positions for an address.

    Prefer data-api HTTP (robust). SDK used only for optional enrichment.
    Activity via SDK is skipped — malformed ConversionActivity rows crash the client.
    """
    import urllib.parse
    import urllib.request

    cfg = cfg or load_config()
    data_cfg = cfg.get("data", {})
    max_trades = int(data_cfg.get("max_trades", 500))
    max_pos = int(data_cfg.get("max_positions", 200))
    max_closed = int(data_cfg.get("max_closed", 200))
    addr = address.strip().lower()

    def http_json(path: str) -> Any:
        url = f"https://data-api.polymarket.com{path}"
        req = urllib.request.Request(
            url,
            headers={
                "accept": "application/json",
                "user-agent": "Mozilla/5.0 (compatible; poly-copy/0.1)",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    trades: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    activity: list[dict[str, Any]] = []
    portfolio_value = 0.0
    traded_count = 0
    lb_pnl: float | None = None
    lb_vol: float | None = None
    profile: dict[str, Any] | None = None

    try:
        raw = http_json(f"/trades?user={urllib.parse.quote(addr)}&limit={min(max_trades, 500)}")
        if isinstance(raw, list):
            for t in raw[:max_trades]:
                ts = t.get("timestamp")
                if str(ts or "").isdigit():
                    ts_out = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                else:
                    ts_out = ts
                trades.append(
                    {
                        "side": t.get("side"),
                        "size": _dec(t.get("size")),
                        "price": _dec(t.get("price")),
                        "timestamp": ts_out,
                        "title": t.get("title"),
                        "slug": t.get("slug"),
                        "event_slug": t.get("eventSlug"),
                        "outcome": t.get("outcome"),
                        "condition_id": t.get("conditionId"),
                        "token_id": str(t.get("asset") or "") or None,
                        "transaction_hash": t.get("transactionHash"),
                    }
                )
    except Exception:
        trades = []

    try:
        raw = http_json(f"/positions?user={urllib.parse.quote(addr)}&limit={min(max_pos, 100)}")
        if isinstance(raw, list):
            for p in raw[:max_pos]:
                positions.append(
                    {
                        "condition_id": p.get("conditionId"),
                        "size": _dec(p.get("size")),
                        "avg_price": _dec(p.get("avgPrice")),
                        "current_value": _dec(p.get("currentValue")),
                        "initial_value": _dec(p.get("initialValue")),
                        "cash_pnl": _dec(p.get("cashPnl")),
                        "realized_pnl": _dec(p.get("realizedPnl")),
                        "percent_pnl": p.get("percentPnl"),
                        "title": p.get("title"),
                        "slug": p.get("slug"),
                        "event_slug": p.get("eventSlug"),
                        "outcome": p.get("outcome"),
                        "cur_price": _dec(p.get("curPrice")),
                    }
                )
    except Exception:
        positions = []

    try:
        # NOTE: /closed-positions defaults to realizedPnl-descending and caps
        # pages at 50 rows, so a single page returns only the biggest winners
        # — that skewed win_rate to 1.0 and zeroed gross losses (which in turn
        # made profit_factor hit its placeholder constant). Paginate with an
        # explicit sort to retrieve the full (or max_closed-capped) set.
        raw: list[Any] = []
        offset = 0
        while len(raw) < max_closed:
            page = http_json(
                f"/closed-positions?user={urllib.parse.quote(addr)}"
                f"&sortBy=realizedpnl&sortDirection=asc&limit=50&offset={offset}"
            )
            if not isinstance(page, list) or not page:
                break
            raw.extend(page)
            if len(page) < 50:
                break
            offset += 50
        if isinstance(raw, list):
            for p in raw[:max_closed]:
                ts = p.get("timestamp")
                closed.append(
                    {
                        "condition_id": p.get("conditionId"),
                        "avg_price": _dec(p.get("avgPrice")),
                        "total_bought": _dec(p.get("totalBought")),
                        "realized_pnl": _dec(p.get("realizedPnl")),
                        "cur_price": _dec(p.get("curPrice")),
                        "timestamp": (
                            datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
                            if str(ts or "").isdigit()
                            else ts
                        ),
                        "title": p.get("title"),
                        "slug": p.get("slug"),
                        "event_slug": p.get("eventSlug"),
                        "outcome": p.get("outcome"),
                    }
                )
    except Exception:
        closed = []

    try:
        vals = http_json(f"/value?user={urllib.parse.quote(addr)}")
        if isinstance(vals, list) and vals:
            portfolio_value = _dec(vals[0].get("value"))
    except Exception:
        portfolio_value = sum(float(p.get("current_value") or 0) for p in positions)

    try:
        traded = http_json(f"/traded?user={urllib.parse.quote(addr)}")
        if isinstance(traded, dict):
            traded_count = int(traded.get("traded") or 0)
    except Exception:
        traded_count = len({p.get("condition_id") for p in positions if p.get("condition_id")})

    # optional SDK enrichment (never required)
    try:
        from polymarket import PublicClient

        with PublicClient() as client:
            try:
                entries = list(
                    client.list_trader_leaderboard(
                        user=addr, time_period="ALL", page_size=5
                    ).iter_items()
                )
                if entries:
                    lb_pnl = _dec(entries[0].pnl)
                    lb_vol = _dec(entries[0].vol)
            except Exception:
                pass
            try:
                prof = client.get_public_profile(addr)
                if prof is not None:
                    profile = {
                        "name": getattr(prof, "name", None),
                        "pseudonym": getattr(prof, "pseudonym", None),
                        "bio": getattr(prof, "bio", None),
                    }
            except Exception:
                pass
    except Exception:
        pass

    return WalletSnapshot(
        address=addr,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        trades=trades,
        positions=positions,
        closed_positions=closed,
        activity=activity,
        portfolio_value=portfolio_value,
        traded_market_count=traded_count,
        leaderboard_pnl=lb_pnl,
        leaderboard_vol=lb_vol,
        profile=profile,
    )


def default_store(cfg: dict[str, Any] | None = None) -> WalletStore:
    cfg = cfg or load_config()
    return WalletStore(resolve_cache_dir(cfg))
