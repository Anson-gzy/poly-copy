/**
 * Cloudflare Worker: Polymarket wallet paper-copy (Fixed + slippage).
 * Cron polls data-api; stores last cursor + recent fills in KV.
 * ponytail: no score/portfolio/risk here — Python CLI keeps research; Worker only follows fills.
 */

export interface Env {
  STATE: KVNamespace;
  WALLETS: string;
  FIXED_NOTIONAL: string;
  SLIPPAGE_BPS: string;
  FOLLOW_SELLS: string;
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
};

const DATA = "https://data-api.polymarket.com";

function num(v: unknown, fallback = 0): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function wallets(env: Env): string[] {
  return env.WALLETS.split(",")
    .map((w) => w.trim().toLowerCase())
    .filter(Boolean);
}

async function fetchTrades(wallet: string, limit = 50): Promise<Trade[]> {
  const url = `${DATA}/trades?user=${wallet}&limit=${limit}`;
  const res = await fetch(url, { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`trades ${res.status} ${wallet}`);
  const data = (await res.json()) as Trade[] | { data?: Trade[] };
  return Array.isArray(data) ? data : data.data ?? [];
}

function paperFill(t: Trade, env: Env): PaperFill | null {
  const side = String(t.side || "").toUpperCase();
  if (side !== "BUY" && side !== "SELL") return null;
  if (side === "SELL" && env.FOLLOW_SELLS !== "true") return null;

  const price = num(t.price, 0.5) || 0.5;
  const slip = num(env.SLIPPAGE_BPS, 500) / 10000;
  const fillPrice = side === "BUY" ? price * (1 + slip) : price * (1 - slip);
  const notional = num(env.FIXED_NOTIONAL, 25);
  const fillSize = notional / fillPrice;
  const ts = num(t.timestamp);

  return {
    wallet: String(t.proxyWallet || "").toLowerCase(),
    side,
    market: String(t.slug || t.title || t.conditionId || ""),
    fillPrice,
    fillSize,
    notional,
    ts,
    tx: t.transactionHash,
  };
}

async function syncWallet(wallet: string, env: Env): Promise<{ newFills: number }> {
  const cursorKey = `cursor:${wallet}`;
  const fillsKey = `fills:${wallet}`;
  const lastTs = num(await env.STATE.get(cursorKey), 0);

  const trades = await fetchTrades(wallet);
  // API usually newest-first; keep only newer than cursor
  const fresh = trades
    .filter((t) => num(t.timestamp) > lastTs)
    .sort((a, b) => num(a.timestamp) - num(b.timestamp));

  if (!fresh.length) return { newFills: 0 };

  const fills: PaperFill[] = [];
  for (const t of fresh) {
    const f = paperFill(t, env);
    if (f) fills.push(f);
  }

  const prevRaw = await env.STATE.get(fillsKey);
  const prev: PaperFill[] = prevRaw ? JSON.parse(prevRaw) : [];
  const merged = [...prev, ...fills].slice(-200);
  await env.STATE.put(fillsKey, JSON.stringify(merged));
  await env.STATE.put(cursorKey, String(num(fresh[fresh.length - 1].timestamp)));

  return { newFills: fills.length };
}

async function runAll(env: Env) {
  const out: Record<string, unknown> = {};
  for (const w of wallets(env)) {
    out[w] = await syncWallet(w, env);
  }
  return out;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);

    if (url.pathname === "/health") {
      return Response.json({ ok: true, wallets: wallets(env) });
    }

    if (url.pathname === "/run" && req.method === "POST") {
      const result = await runAll(env);
      return Response.json({ ok: true, result });
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
      endpoints: ["GET /health", "POST /run", "GET /fills?wallet=", "cron */5"],
    });
  },

  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(runAll(env));
  },
};
