# poly-copy

Polymarket **钱包组合跟单**策略（第一期：纸面跟单，不下真单）。

位置：`strategies/poly-copy`（策略代码；底层 SDK 在 `tool/Polymarket-py-sdk`）。

核心范式：钱包 = 可交易标的；可靠度评分 + 组合配置 + 止损/漂移/轮换。

## 布局

```text
strategies/poly-copy/
  configs/default.yaml
  src/poly_copy/
    data/ features/ score/ portfolio/ copy/ risk/ backtest/
    cli.py
  tests/
  cache/          # 钱包快照 JSON
```

## 环境

```bash
cd strategies/poly-copy
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e "../../tool/Polymarket-py-sdk" --pre
pip install -e ".[dev]"
```

## CLI 验收

案例钱包：`0x25e28169faea17421fcd4cc361f6436d1e449a09`

```bash
# 拉公开数据并缓存
poly-copy fetch --wallet 0x25e28169faea17421fcd4cc361f6436d1e449a09

# 特征 + 分数 + 适合/不适合
poly-copy screen --wallet 0x25e28169faea17421fcd4cc361f6436d1e449a09

# 组合权重
poly-copy score --wallets 0x25e2... 0x...

# 纸面跟单日志（含风控）
poly-copy paper --wallet 0x25e28169faea17421fcd4cc361f6436d1e449a09 --mode fixed

# 组合报告：权重、赛道分散、模拟净值与最大回撤
poly-copy report --wallet 0x25e28169faea17421fcd4cc361f6436d1e449a09

# 参数扫描
poly-copy backtest --wallet 0x25e28169faea17421fcd4cc361f6436d1e449a09 --scan
```

或：`python -m poly_copy.cli ...`

## 单测

```bash
pytest -q
```

覆盖：黑名单规则、Fixed sizing、单钱包止损。

## 模块契约

`WalletEvent` → `WalletFeatures` → `WalletScore` → `Allocation` → `CopyIntent` → `RiskDecision` → `PaperFill`

## 明确不做（第一期）

- 实盘下单 / Kreo / Telegram bot
- 私钥接法预留：沿用 `tool/Polymarket-py-sdk/.env.account.example`

## 跟单速度与流动性

当前是**纸面程序推进**，不是实盘。云端只用 GitHub Actions。

| 路径 | 频率 | 用途 |
|---|---|---|
| `poly-copy watch` | 默认 15s 轮询 | 本地纸面跟单（流动性门控） |
| GitHub `poly-copy-watch` | 每 5 分钟 | 免费云端纸面快跟 |
| `poly-copy paper/report` | 按需 / 30m | 研究回放 |

流动性规则（选钱包 + 跟单）：
- 盘口 `liquidity` ≥ **$10k**
- 标的钱包该笔成交名义 / 盘口深度 ≤ **15%**（避免「只有他一笔」）
- 钱包层面：多数成交在深盘、中位深度够、不长期主导盘口

```bash
poly-copy watch --wallet 0x25e2... --interval 15
poly-copy screen --wallet 0x25e2...   # 含流动性特征与黑名单
```

## GitHub Actions（免费定时纸面跟单）

仓库已带 workflow：

- `poly-copy-watch.yml`：每 5 分钟流动性门控快跟
- `poly-copy-paper.yml`：每 30 分钟研究快照（fetch/screen/paper/report）

也可在 Actions 页手动 **Run workflow**；输出在 Artifacts。  
可选：Repo → Settings → Variables 设 `POLY_WALLET`。

## Dashboard（Geist HTML）

状态与历史看板：`dashboard/index.html`

```bash
poly-copy dashboard --wallet 0x25e28169faea17421fcd4cc361f6436d1e449a09
python -m http.server 8787 --directory dashboard
# open http://127.0.0.1:8787
```

页面展示：当前 verdict / PnL / 纸面净值、评分拆解、赛道分散、最近成交、GitHub Actions 运行历史。

## 发现钱包（教程筛选）

替代 polymarketanalytics：公开 leaderboard → 教程硬筛。

```bash
poly-copy discover --candidates 80 --limit 20
poly-copy universe sync    # 校验现有成员；不足 10 个则重扫并挑最好的补齐；系统配权
poly-copy universe show
poly-copy paper --universe --mode portfolio   # 同时跟组合内全部钱包
poly-copy watch --universe --mode portfolio --once
```

组合规则：
- 目标 **10** 个合格钱包，分数加权分配仓位
- **入选**用 `hard_screen`（教程筛参，偏严）
- **剔除**用 `exit_screen`（更难触发 + 连续 2 次不达标才踢），避免擦线就换钱包
- 活跃数 < 10 时自动 discover，按评分选最好的补齐

## 持久化纸面账本与风控纪律

跨 CI 运行累积的纸面账户状态（起始资金 **1000 USDC**），由 watch/paper 增量维护：

- `dashboard/ledger.json`：现金、当前持仓（token → size/avg_price/source_wallet/domain/opened_at）、
  每个源钱包的**处理游标**（last_seen 时间戳 + 同秒 tx hash 去重）、累计已实现 PnL、
  高水位、`halted`/`would_halt` 熔断标志、钱包淘汰记录、append-only 的 `fills` 成交日志
  （source/domain/slippage/pnl/`reason`，用于 `poly-copy attribution` 归因）
- `dashboard/equity.json`：追加式净值序列 `[{ts, equity, cash, positions_value, n_open, realized_pnl_cum}]`，
  滚动保留最近 2000 个点

工作方式：每次运行只处理游标之后的**新成交**（账本不存在时冷启动，游标回看
`ledger.bootstrap_lookback_hours`，默认 6 小时，避免重放全部历史）；持仓用 Gamma
盘口 mid 估值（取不到价保留上次估值并标 `mark_stale`）；市场 resolve 后按结算价
自动转已实现。`poly-copy-watch.yml` 会把两个文件随 universe 一起 commit。

风控规则（`configs/default.yaml` 的 `risk` 段）：

1. **仓位公式**：单笔跟单名义 = min(成员 weight × 当前 equity × 对方该笔占其组合比例, `per_trade_cap` $50)，
   单一源钱包合计敞口 ≤ equity 的 `wallet_exposure_cap` 10%
2. **组合熔断（模拟盘阶段：只记录不停机）**：equity 从高水位回撤 ≥ `portfolio_halt_drawdown` 15%
   → `ledger.json` 写 `would_halt=true` + `would_halt_reason`，**继续正常跟单**，不阻塞新开仓。
   `halted` 字段保留、恒为 `false`（除非 `risk.halt_enabled: true`）。这是 2026-07-18 的决策：
   上线 6 天净值 -18.5% 触发旧版硬熔断后空转 6 天，模拟盘阶段亏损不是真实资金损失，比起被熔断
   闷杀更需要继续采集数据、验证参数（见下方 `poly-copy attribution` / `backtest --grid`）。
   要在实盘/真金模式恢复"回撤即停"的硬约束，把 `configs/default.yaml` 的 `risk.halt_enabled`
   改成 `true`（`halted` 届时依然是 sticky：解除需手动把 `halted` 改回 `false`）
3. **单钱包淘汰**：某源钱包贡献的 PnL 从峰值回撤超过起始资金的
   `wallet_evict_drawdown` 20% → 立即踢出 universe 并按市价平掉其纸面持仓（记录 reason）
4. **行为漂移 strike**：与入池 baseline（存于 universe 成员 `baseline` 字段）相比 —
   赛道 Jaccard < 0.4、月频 >2x 或 <0.5x、单笔名义中位数 >3x，各记 1 strike，
   与 exit_screen 失败共用 `exit_strikes`，累计 2 次剔除
5. **新钱包隔离期**：入池 `quarantine_days` 7 天内权重减半（tags 带 `quarantine`），期满自动转正
6. **赛道集中度**：单一 domain 合计权重 ≤ `domain_weight_cap` 40%，超出部分按比例压缩并归一化给其他 domain

硬筛对齐（`hard_screen` + `blacklist`）：PnL $15k–$400k、持仓 ≥ $5k、活跃仓位 ≥ 2、
交易数 ≥ 20、胜率 ≥ 70%、月频 30–200；黑名单硬拒：月频 >200 或分钟级连发
（60s 窗口 >8 笔）、单事件 PnL 占比 >50%、低流动性交易员（liquid_trade_share < 0.5）。

`poly_copy/discover.py` 的 probe（`_get`）对 data-api 请求失败会指数退避重试 3 次
（429/5xx/超时，基础延迟 1s 翻倍）；胜率相关的 `/closed-positions` 若样本为 0 或
请求最终仍失败，`win_rate` 记为 `None` 并标 `data_unavailable`，而不是当作真实 0%
胜率去硬拒/记 strike。`universe sync` 的 exit 重核验遇到 `data_unavailable` 时跳过
该成员本轮全部硬拒检查、不计 strike、原样保留并打 `data_stale` tag；新钱包入池
（`hard_screen`）遇到 `data_unavailable` 则直接拒绝（宁缺毋滥）。

## 归因报告

```bash
poly-copy attribution   # 读 dashboard/ledger.json + equity.json，写 dashboard/attribution.json
```

按源钱包 / 按赛道（domain）统计已实现 PnL、成交笔数、滑点成本、PnL 占比；汇总滑点
总成本占总亏损的比例；按开仓时距市场 `endDate`（Gamma）分桶（<1h / 1-6h / 6-24h / >24h）
统计笔数与 PnL；统计因 universe 换手强平（`reason=evict_universe_churn`）贡献的已实现 PnL。
细粒度归因依赖 `ledger["fills"]` 成交日志（本次改动新增，含 source/domain/slippage/reason）；
在此之前的历史区间没有逐笔记录，报告会明确标注 `data_source` 并退化为
`ledger["wallet_realized"]` 的聚合数字（仍是真实数据，只是没有赛道/滑点明细）。

## 参数网格回测（回放真实成交历史）

```bash
poly-copy backtest --grid                 # 写 dashboard/backtest_grid.json
poly-copy backtest --grid --grid-limit 500  # 每个钱包多拉一些历史成交
```

对 universe 当前成员 + ledger 里出现过的所有源钱包，重新拉取公开成交历史，在
`fixed_notional 上限 [10,20,25,50] × stop_loss [0.5,0.7,0.85] × 距结算过滤
[0,2h,6h] × 模拟延迟 [0s,60s,300s]` 的 4×3×3×3=108 组参数上逐组重放（复用
`ledger.ingest_events`/`settle_resolved`，不是 `backtest.run_backtest` 的启发式
估算 PnL）。距结算过滤用 Gamma `endDate`，未知则保守放行；延迟档位没有逐笔盘口
可回放，用固定滑点惩罚（0/1%/2.5%）近似"更慢成交吃到的额外不利价差"，`max_dd`
只看已实现 PnL 的回撤（同样是没有逐笔盘口序列的近似），`total_pnl`/`final_equity`
则按当前真实盘口给未平仓头寸估值。输出仿 vectorbt 风格：每组参数一行指标。
