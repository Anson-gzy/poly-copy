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

## GitHub Actions（免费定时纸面跟单）

仓库已带 workflow：`.github/workflows/poly-copy-paper.yml`

- 每小时 UTC 跑一次：`fetch` → `screen` → `paper` → `report`
- 也可在 Actions 页手动 **Run workflow**
- 输出在 Artifacts：`poly-copy-out`
- 可选：Repo → Settings → Variables 设 `POLY_WALLET`

本地推送后启用：

```bash
cd strategies/poly-copy
git push
# GitHub → Actions → poly-copy paper → Enable / Run workflow
```

## Cloudflare Worker

边缘定时纸面跟单（Fixed）：见 [`worker/README.md`](worker/README.md)。
评分/组合/回测仍用本目录 Python CLI。
