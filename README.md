# Meridian Crypto Research

项目源码采用 [MIT 许可证](LICENSE)。随附的 Inter 字体采用 [SIL Open Font License 1.1](web/Inter-LICENSE.txt)。

面向投资决策的三产品研究系统。三类产品共享数据与证据基础设施，但使用独立 schema、pipeline、prompt 与 renderer：

- 北京时间次日 00:10：Daily Market Strategy，复盘前一个完整自然日并判断未来 24–72H
- 北京时间 08:00、20:00：Tactical Monitor，强制刷新最新行情并给出 BTC/ETH/SOL 趋势与完整条件交易计划
- 北京时间 10:00：准备跨 Web3、链上、AI、资本流动性和股票商业的研究资料包，不播报
- 北京时间 10:15：本地开始写作与独立审稿；12:00 发布深度文章
- 北京时间 14:00：重试未完成稿件；缺少材料时暂缓发布，保留上一篇

`/reports` 只展示简报和日度研报；`/insights` 展示深度文章、主题图表、事实引用和来源。未保存简报/日报保留 3 天，未保存深度文章保留 7 天，已保存内容长期保留。

## 本地验收

在仓库根目录运行 `.\.venv\Scripts\python.exe -m backend.app.local_preview`，打开 http://127.0.0.1:8115/。
验收库位于 `output/acceptance/desk`，备用稿在该库的 `desk/ready` 中。增加 `--scheduled` 后启用本机定时采集、备稿和发布。
首页展示有效期内的研究；`/terminal?asset=ETH` 打开以太坊终端，`/reports?id=…` 和 `/insights?id=…` 可分享具体版本。
只读接口 `/api/home` 提供产品时点、头条、图表及归档链接。单期采集、编辑和入库进度可在本地库的 `jobs` 下按 publication_key 查询。

## 前端开发与验收

四个页面由 FastAPI 直接提供，独立运行时无需打包。共享导航及首页 ES 模块在 `web/`，全站设计变量在 `desk.css`。首页资产卡读取 `/api/home` 的 `market` 快照，比较图从 `/api/market/candles` 读取 Coinbase 已收盘数据。24H、7D、30D 分别使用 15 分钟、1 小时、6 小时蜡烛；三个资产在同一时点的开盘价分别归零，中间缺柱断线。现货卡与图表各自标明时间和交易场所。

在根目录运行 `node --test tests/performance.test.mjs` 验证窗口和缺失行情，运行 `npx --package typescript@5.9.3 tsc -p web/tsconfig.json` 检查共享模块与首页类型。

## 研究与生成规则

- Daily 与 Tactical 使用独立 schema、生成和页面；深度文章使用材料驱动的整篇写作、独立审稿与整篇修订。
- Pro 采用流式响应，生成保护 260 秒，函数总任务保护 275 秒；完整稿输出上限为 20,000 Token。
- 深度文章不设硬性字数上下限，通常建议 3,500–6,000 字；字数不参与发布判定，也不发布删减保底版。
- 事实引用逐段核验，纯过渡允许空引用；支持声明来源、假设、单位和公式的透明演算，结果由程序复核。
- 公开网页抽取限制大小和重定向并阻止私网地址；付费墙正文不用作证据。有界来源正文存于内部资料包供核验，阅读页不重新发布来源全文。
- Daily/Tactical 保持定时发布和缺失数据披露。深度文章提前写作；发刊时优先使用当期已审稿，其次选择来源复核通过的原创备用稿。
- 深度文章根据问题选材与组织叙事，不强制市场共识、分歧、反方或投资含义章节；独立编辑逐段对照原始资料。事实错误须返修，文风意见允许在复修后随稿保留。
- 缺失数据不参与方向计算；情景仅显示 High/Medium/Low，精确概率在完成历史校准前不展示。
- 发布键、revision、CURRENT/SUPERSEDED 状态与存储锁共同防止重复 CURRENT；旧版本仍可按 ID 审计。

## 本地运行

```powershell
pip install -r requirements.txt
python -m backend.app.cli generate
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

页面：

- 研究首页：`http://127.0.0.1:8000`
- 市场终端：`http://127.0.0.1:8000/terminal`
- 研报中心：`http://127.0.0.1:8000/reports`
- 深度洞察：`http://127.0.0.1:8000/insights`

## API

- `GET /api/reports`、`GET /api/reports/{id}`
- `GET /api/reports/{id}/export/markdown|json`
- `PATCH /api/reports/{id}/saved?saved=true|false`、`DELETE /api/reports/{id}`
- `POST /api/reports/generate`、`POST /api/reports/generate/daily`
- `GET /api/insights`、`GET /api/insights/{id}`
- `GET /api/insights/{id}/export/markdown|json`
- `PATCH /api/insights/{id}/saved?saved=true|false`、`DELETE /api/insights/{id}`
- `POST /api/insights/prepare`、`POST /api/insights/generate?short=false`
- `GET /health`、`GET /api/health`、`GET /api/performance?days=90`、`GET /api/deliveries`

手动写操作使用 `X-Meridian-Admin-Token`；Vercel Cron 使用 `Authorization: Bearer $CRON_SECRET`。

## Vercel

Python Function 的 `maxDuration` 为 300 秒。生产环境在 UTC 16:10 生成前一个北京自然日 Daily，UTC 00:00/12:00 生成 08:00/20:00 Tactical。Hobby 部署只在五个关键发布窗口后运行 reconcile（UTC 00:35、04:35、06:35、12:35、16:35），用于补偿 Daily、Deep 和当前有效 Tactical，同时避免高频 Blob 列表操作耗尽免费额度。

Vercel 环境关闭进程内 APScheduler，并使用 Vercel Blob 持久化报告、任务、缓存与导出文件。新建的 Blob 连接默认使用 Vercel 自动注入的 OIDC 凭据与 `BLOB_STORE_ID`；`BLOB_READ_WRITE_TOKEN` 仅作为旧连接或本地调试的兼容方式。本地开发使用 `./data/` 下的同构 JSON 文件布局。

对象布局：报告为 `reports/{symbol}/{report_id}.json`，任务为 `jobs/{job_id}.json`，缓存为 `cache/{provider}/{resource}.json`（包含 `expires_at`），导出物为 `exports/{report_id}.md|pdf`。Blob 故障不会阻断应用启动；依赖存储的 API 会返回 `STORAGE_UNAVAILABLE` 结构化 503。

## 主要文件

- `backend/app/insights.py`：安全正文抽取、相关性聚类与可核验资料包
- `backend/app/editorial.py`、`deep_writing.py`、`article_policy.py`、`article_calculations.py`：产品编辑、整篇写作与审稿、发布门槛和透明计算
- `backend/app/service.py`：日期锁、准备、生成、重试和发布编排
- `backend/app/storage.py`：统一 `ReportStore` 接口和 JSON 文档语义
- `backend/app/storage_blob.py`：Vercel Blob 生产实现
- `backend/app/storage_local.py`：`./data/` 本地文件实现
- `insights.html / insights.css / insights.js`：深度洞察阅读与管理
- `reports.html / reports.css / reports.js`：简报与日报中心
- `tests/test_pipeline.py`：采集、模型路由、校验、幂等调度和保留策略测试
