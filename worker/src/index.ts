/**
 * Fast paper-copy Worker: 1-min cron, parallel wallets, liquidity gate.
 * Skip thin books and leader-dominated fills (not "only their trade").
 */

export interface Env {
  STATE: KVNamespace;
  WALLETS: string;
  FIXED_NOTIONAL: string;
  SLIPPAGE_BPS: string;
  FOLLOW_SELLS: string;
  MIN_LIQUIDITY: string;
  MAX_TRADE_LIQ_SHARE: string;
}

type Trade = {
  proxyWallet?: string;
  side?: string;
  size?: number | string;
  price?: number | string;
  timestamp?: number | string;
  title?: string;
  slug?: string;
  eventSlug?: string;
  outcome?: string;
  transactionHash?: string;
  conditionId?: string;
};

type PaperFill = {
  wallet: string;
  side: string;
  market: string;
  fillPrice: number;
  fillSize: number;
  notional: number;
  ts: number;
  tx?: string;
  liquidity?: number;
  skip?: string;
};

const DATA = "https://data-api.polymarket.com";
const GAMMA = "https://gamma-api.polymarket.com";

function num(v: unknown, fallback = 0): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function wallets(env: Env): string[] {
  return env.WALLETS.split(",")
    .map((w) => w.trim().toLowerCase())
    .filter(Boolean);
}

async function fetchTrades(wallet: string, limit = 20): Promise<Trade[]> {
  const url = `${DATA}/trades?user=${encodeURIComponent(wallet)}&limit=${limit}`;
  const res = await fetch(url, { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`trades ${res.status} ${wallet}`);
  const data = (await res.json()) as Trade[] | { data?: Trade[] };
  return Array.isArray(data) ? data : data.data ?? [];
}

async function marketLiquidity(slug?: string, conditionId?: string): Promise<number | null> {
  try {
    if (slug) {
      const res = await fetch(`${GAMMA}/markets?slug=${encodeURIComponent(slug)}`, {
        headers: { accept: "application/json" },
      });
      if (res.ok) {
        const rows = (await res.json()) as Array<{ liquidity?: string | number }>;
        if (Array.isArray(rows) && rows[0]) {
          const liq = num(rows[0].liquidity);
          if (liq > 0) return liq;
        }
      }
    }
    if (conditionId) {
      const res = await fetch(
        `${GAMMA}/markets?condition_ids=${encodeURIComponent(conditionId)}`,
        { headers: { accept: "application/json" } },
      );
      if (res.ok) {
        const rows = (await res.json()) as Array<{ liquidity?: string | number }>;
        if (Array.isArray(rows) && rows[0]) {
          const liq = num(rows[0].liquidity);
          if (liq > 0) return liq;
        }
      }
    }
  } catch {
    return null;
  }
  return null;
}

function liqOk(tradeNotional: number, liquidity: number | null, env: Env): string | null {
  const minLiq = num(env.MIN_LIQUIDITY, 10_000);
  const maxShare = num(env.MAX_TRADE_LIQ_SHARE, 0.15);
  if (liquidity == null) return "liq_unknown";
  if (liquidity < minLiq) return `liq_thin:${liquidity}`;
  if (liquidity > 0 && tradeNotional / liquidity > maxShare) {
    return `liq_dominated:${(tradeNotional / liquidity).toFixed(2)}`;
  }
  return null;
}

async function paperFill(t: Trade, env: Env): Promise<PaperFill | null> {
  const side = String(t.side || "").toUpperCase();
  if (side !== "BUY" && side !== "SELL") return null;
  if (side === "SELL" && env.FOLLOW_SELLS !== "true") return null;

  const price = num(t.price, 0.5) || 0.5;
  const size = num(t.size);
  const tradeNotional = size * price;
  const slug = String(t.slug || t.eventSlug || "");
  const liquidity = await marketLiquidity(slug || undefined, t.conditionId);
  const skip = liqOk(tradeNotional, liquidity, env);
  if (skip) {
    return {
      wallet: String(t.proxyWallet || "").toLowerCase(),
      side,
      market: slug || String(t.conditionId || ""),
      fillPrice: 0,
      fillSize: 0,
      notional: 0,
      ts: num(t.timestamp),
      tx: t.transactionHash,
      liquidity: liquidity ?? undefined,
      skip,
    };
  }

  const slip = num(env.SLIPPAGE_BPS, 500) / 10000;
  const fillPrice = side === "BUY" ? price * (1 + slip) : price * (1 - slip);
  const notional = num(env.FIXED_NOTIONAL, 25);
  return {
    wallet: String(t.proxyWallet || "").toLowerCase(),
    side,
    market: slug || String(t.title || t.conditionId || ""),
    fillPrice,
    fillSize: notional / fillPrice,
    notional,
    ts: num(t.timestamp),
    tx: t.transactionHash,
    liquidity: liquidity ?? undefined,
  };
}

async function syncWallet(wallet: string, env: Env): Promise<{ newFills: number; skipped: number }> {
  const cursorKey = `cursor:${wallet}`;
  const fillsKey = `fills:${wallet}`;
  const lastTs = num(await env.STATE.get(cursorKey), 0);

  const trades = await fetchTrades(wallet, 20);
  const fresh = trades
    .filter((t) => num(t.timestamp) > lastTs)
    .sort((a, b) => num(a.timestamp) - num(b.timestamp));

  if (!fresh.length) return { newFills: 0, skipped: 0 };

  const fills: PaperFill[] = [];
  let skipped = 0;
  for (const t of fresh) {
    const f = await paperFill(t, env);
    if (!f) continue;
    if (f.skip) {
      skipped += 1;
      continue;
    }
    fills.push(f);
  }

  if (fills.length) {
    const prevRaw = await env.STATE.get(fillsKey);
    const prev: PaperFill[] = prevRaw ? JSON.parse(prevRaw) : [];
    await env.STATE.put(fillsKey, JSON.stringify([...prev, ...fills].slice(-200)));
  }
  await env.STATE.put(cursorKey, String(num(fresh[fresh.length - 1].timestamp)));
  return { newFills: fills.length, skipped };
}

async function runAll(env: Env) {
  const list = wallets(env);
  const results = await Promise.all(
    list.map(async (w) => [w, await syncWallet(w, env)] as const),
  );
  return Object.fromEntries(results);
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/health") {
      return Response.json({ ok: true, wallets: wallets(env), cron: "* * * * *" });
    }
    if (url.pathname === "/run" && req.method === "POST") {
      return Response.json({ ok: true, result: await runAll(env) });
    }
    if (url.pathname === "/fills") {
      const wallet = (url.searchParams.get("wallet") || wallets(env)[0] || "").toLowerCase();
      const raw = await env.STATE.get(`fills:${wallet}`);
      return Response.json({
        wallet,
        fills: raw ? JSON.parse(raw) : [],
        cursor: await env.STATE.get(`cursor:${wallet}`),
      });
    }
    return Response.json({
      service: "poly-copy-paper",
      endpoints: ["GET /health", "POST /run", "GET /fills?wallet=", "cron every 1m"],
    });
  },

  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(runAll(env));
  },
};
