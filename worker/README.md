# poly-copy Cloudflare Worker（纸面跟单）

Workers **不能**直接跑 Python 全套评分/组合；这里只把 **Fixed 纸面跟单轮询**接到边缘：

- Cron 每 5 分钟拉 `data-api.polymarket.com/trades`
- 按 `FIXED_NOTIONAL` + `SLIPPAGE_BPS` 模拟成交
- 游标与最近 fills 存 KV
- 研究/筛钱包/回测仍用上级目录的 Python CLI

## 一次部署

```bash
cd strategies/poly-copy/worker
npm i
npx wrangler kv namespace create poly-copy-state
npx wrangler kv namespace create poly-copy-state --preview
# 把返回的 id 填进 wrangler.toml 的 id / preview_id
npx wrangler deploy
```

本地：

```bash
npx wrangler dev
curl http://127.0.0.1:8787/health
curl -X POST http://127.0.0.1:8787/run
curl 'http://127.0.0.1:8787/fills?wallet=0x25e28169faea17421fcd4cc361f6436d1e449a09'
```

改跟单钱包：编辑 `wrangler.toml` 的 `WALLETS`（逗号分隔），或 Dashboard → Variables。

## 明确不做

- 实盘下单、私钥、完整硬筛/评分（仍在 Python）
