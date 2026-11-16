# 运行与数据治理

## 当前可运行能力

- OKX：主行情源，提供 15M/1H/北京日 K、24H 价格与成交额、资金费率、标记/指数价差和 OI。
- Bybit：交叉验证未平仓量及 1H 变化。
- Binance/Coinbase：OKX 失败时的价格与 K 线回退。
- CoinGecko：全市场总市值、成交量、BTC 市占率、市值前 100 宽度和跨源价格校验。
- Alternative.me：Crypto Fear & Greed Index，报告中保留来源归属。
- 爆仓：可选 CoinGlass/专业网关；未授权时显示 `UNAVAILABLE`，不将公开实时流冒充全市场 24H 总额。
- Google News RSS：事件发现；来源分低于 72 的线索不进入方向判断。
- DefiLlama：ETH/SOL 链级 TVL、24 小时费用与稳定币供给；明确区分链级费用和代币持有者收入。
- Blockchain.com Charts：BTC 算力、难度和日交易手续费。
- Deribit：BTC/ETH 期权名义持仓、Put/Call OI、近 ATM IV 与期限结构。
- 每 5 分钟保存市场快照，每 15 分钟保存研究上下文；北京时间次日 00:10 前一日 Daily、08:00/20:00 Tactical、10:00 资料包、12:00 深度洞察、14:00 失败重试。
- Tactical 发布前强制现场采集。失败时使用最近可用快照并按实际时效标记；价格超过 15 分钟的计划不可显示为已触发。研究上下文回退最多 60 分钟。
- 报告满 24 小时后自动结算方向、目标命中、MFE、MAE 和 Brier Score。
- 未保存简报/日报保留三天，未保存深度洞察保留七天；已保存内容长期保留。

## 本地生产式运行

```powershell
pip install -r requirements.txt
python -m backend.app.cli generate
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

公网部署需要将 Blob Store 关联到项目（Vercel 自动注入 OIDC 与 `BLOB_STORE_ID`），并设置 `MERIDIAN_ADMIN_TOKEN` 和 `CRON_SECRET`。`BLOB_READ_WRITE_TOKEN` 仅用于旧式 Blob 连接或本地调试。手动生成报告时通过 `X-Meridian-Admin-Token` 请求头传入；Vercel Cron 使用 `Authorization: Bearer $CRON_SECRET`。

## Docker

```powershell
docker compose up --build -d
docker compose logs -f meridian
```

本地 JSON 数据保存在命名卷 `meridian-data`。Vercel 生产环境关闭进程内调度器，由 Vercel Cron 调用 API；任务状态和锁均保存在 Blob 对象中。

## 发布检查

1. `GET /health` 返回且仅返回 `{\"status\":\"ok\",\"service\":\"research\"}`。
2. `POST /api/reports/generate` 完成一次现场报告。
3. `GET /api/reports/latest` 的三个资产均有 `data_quality` 和 `quality_flags`。
4. Tactical 三个资产均有方向判断；方向不明时同时存在完整条件多单与空单；所有计划通过入场、止损和三档止盈几何校验。
5. `GET /api/reports/latest/markdown` 可直接用于下游分发。
6. `/reports` 可完成全文查看、保存、删除和 Markdown/JSON 导出。
7. `/insights` 可完成长文、图表、引用链接、保存与导出；不设字数硬下限或上限，通常建议 3,500–6,000 字，须通过独立编辑核验。
8. 监控上游 429/5xx、V4.1 Flash 耗时、失败状态、缓存回退次数和证据覆盖度。
9. Daily 的 `session_window` 必须是前一个北京自然日，页面不得出现 `Preferred Trades`/`NO_TRADE`。

## 当前明确缺口

- 授权 ETF 日度净流、CME 基差和标准 25D Skew 需要配置专业数据网关。
- 交易所净流、大额实体流与长期持有者结构需要授权实体标签数据。
- BTC 矿工实体流与长期持有者、ETH 质押与燃烧、SOL 活跃实体与验证者健康度。
- 公开新闻已进行正文抽取、事件聚类和来源去重；付费数据库与付费新闻仍未接入。
- 24H 结算已实现；3D/7D 校准和分市场状态统计尚待扩展。

缺口维度不会由替代指标填充。报告会降低覆盖度并直接显示质量提示。

## V4.1 编辑运行

- `MERIDIAN_EDITOR_ENABLED=true` 且 `DEEPSEEK_API_KEY` 存在时启用 DeepSeek；否则 Daily/Tactical 使用确定性稿件，深度文章暂缓。
- 简报、日报和深度洞察统一使用 DeepSeek V4.1 Flash；`MERIDIAN_STANDARD_MODEL` 与 `MERIDIAN_ARTICLE_MODEL` 均默认为官方模型名 `deepseek-flash`。
- 深度洞察使用流式响应，生成保护 260 秒，函数总任务保护 275 秒，完整稿输出上限为 20,000 Token。
- 深度文章只要整篇生成并通过基础结构校验即可发布；编辑意见不再阻断。真正生成失败时保留上一篇并重试，不恢复模板稿。
- `editorial_audit` 记录模型、尝试次数、耗时、Token 和告警，不记录密钥或思维过程。
