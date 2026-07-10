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
- 每次 sync 检测是否仍符合硬筛/黑名单；不合格剔除
- 活跃数 < 10 时自动 discover，按深度评分选最好的补齐
