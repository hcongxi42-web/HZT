# 每日股市简报 — 部署与运行指南

## 项目定位

自动抓取 A 股 / 美股 / 港股新闻与大盘资金面，调用 DeepSeek 生成四路分析（盘面 /
UP 主观点蒸馏 / 信息差提炼 / AI 产业链选股），渲染为一份学术风 HTML 简报，并通过
GitHub Actions 部署到 GitHub Pages。

> 本仓库**不再使用** Dify、Docker、Kimi 等旧组件。整条流水线就是
> `python stock_report.py` + 一个 GitHub Actions 工作流，仅依赖 Python 标准库。

## 架构

```
[GitHub Actions 定时/手动] → python stock_report.py
        │
        ├─ news_fetcher.fetch_all_news_flat()   抓取新闻 + 资金面（库函数）
        ├─ call_llm()                            盘面分析
        ├─ call_opinion_analyzer()               UP 主观点蒸馏（并行）
        ├─ call_info_analyzer()                  信息差提炼（并行）
        ├─ call_stock_picker()                   AI 产业链选股（融合上述三者）
        └─ generate_html_report() → docs/index.html + 历史页 → 提交并部署 Pages
```

## 目录结构

| 文件 / 目录 | 作用 |
|------|------|
| `stock_report.py` | 主程序：抓取 → 四路 LLM 分析 → 渲染 HTML → 部署 |
| `news_fetcher.py` | 新闻 / 资金面抓取（被 stock_report 当作**库**导入，非服务） |
| `utils.py` | 北京时间等通用工具 |
| `prompts/*.txt` | 8 个提示词模板（analyst / stock_picker / opinion / info_gap 各含 system+user） |
| `docs/style.css`、`docs/script.js` | 前端样式与交互，渲染时内联进每份报告 |
| `docs/report_*.html`、`docs/index.html` | 生成的报告（最新报告即 `index.html`） |
| `docs/charts/` | 报告内图表资源 |
| `docs/.nojekyll` | 关闭 GitHub Pages 的 Jekyll 处理 |
| `up主的每日观点/` | **本地输入目录**（不提交）：存放 UP 主观点与信息差转录文本 |

## 前置条件

- Python 3.11（CI 使用 `ubuntu-latest` + Python 3.11）
- 一个 DeepSeek API Key
- 仅在**本地**想导出 PDF 时需要 Chrome / Chromium（CI 不安装，自动跳过）

## 环境变量 / 仓库 Secrets

在 GitHub 仓库 `Settings → Secrets and variables → Actions` 中配置：

| 变量 | 必填 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek 模型调用密钥 |
| `SERVERCHAN_KEY` | ⬜ | Server 酱微信推送（可选） |
| `WEBHOOK_URL` | ⬜ | 企业微信 / 钉钉 Webhook（可选） |

`GITHUB_REPOSITORY` 与 `GITHUB_EVENT_NAME` 由 Actions 自动注入，无需配置。

## 本地运行

```bash
# 1) 测试新闻抓取（不调用 LLM）
python news_fetcher.py test

# 2) 生成完整报告（需设置 DEEPSEEK_API_KEY）
export DEEPSEEK_API_KEY="sk-xxxx"
python stock_report.py
```

本地运行会把报告写到 `docs/index.html`、`docs/report_<日期>.html`、
`docs/report_<日期>_am|pm.html`，并在仓库根目录生成 `report_<时间戳>.md`（该 md 已被
`.gitignore` 忽略，仅作本地存档）。

## 部署（GitHub Actions）

工作流文件：`.github/workflows/daily_stock_report.yml`

- **自动触发**：北京时间 `07:17` 与 `22:17`（对应 UTC `23:17` 前一天与 `14:17`），
  由 Python 内部判断交易日，非交易日跳过提交。
- **手动触发**：在 Actions 页面点击 `Run workflow`（workflow_dispatch）。
- **部署**：报告 HTML 提交回仓库后，自动上传并部署到 GitHub Pages。
- **推送通知**：可选通过 `SERVERCHAN_KEY` / `WEBHOOK_URL` 发送。

首次使用需在仓库 `Settings → Pages` 确认 Pages 源为 `GitHub Actions`。

## 输入：UP 主观点与信息差

将转录好的文本（`.txt`）放入 **`up主的每日观点/`** 目录（支持子目录），文件命名需含
日期与 UP 标识，例如：

```
up主的每日观点/7月6日_擒龙先生.12345678.ai-zh.txt
up主的每日观点/up小A/7月6号_观点.87654321.ai-zh.txt
```

- 程序按文件名中的 `X月X日 / X月X号` 解析日期，按 `.<数字>.ai-zh.txt` 解析 UP 标识。
- **早报**使用「昨日」观点（`date_offset=-1`），**晚报**使用「今日」观点（`date_offset=0`）。
- 找不到匹配文件时，该模块自动降级（盘面分析照常，选股仅融合新闻与信息差）。

## 输出说明

| 产物 | 位置 | 备注 |
|------|------|------|
| 最新报告 | `docs/index.html` | Pages 入口 |
| 当日主报告 | `docs/report_YYYYMMDD.html` | 归档页链接 |
| 早 / 晚报 | `docs/report_YYYYMMDD_am.html` / `_pm.html` | 不互相覆盖 |
| Markdown | `report_YYYYMMDD_HHMM.md`（根目录） | 本地存档，`gitignore` |
| PDF | 仅本地有 Chrome 时生成 `docs/pdf/` | CI 不生成，已 `gitignore` |

## 关于 PDF

CI 运行环境未安装 Chrome，`generate_pdf()` 会打印「未找到 Chrome，跳过 PDF 生成」并
返回。如需在本地获得 PDF，安装 Chrome / Chromium 后直接运行 `python stock_report.py`
即可，生成的 PDF 保存在 `docs/pdf/`（已被 git 忽略，不会进入仓库与 Pages）。

## 已移除的旧组件（历史记录，避免混淆）

以下文件曾服务于 Dify + Docker 架构，现已删除，请勿找回：

- `cron_trigger.py`、`workflow_payload.json` —— Dify 工作流触发与定义
- `Dockerfile`、`docker-compose.yml` —— 仅用于把 news_fetcher 起 HTTP 服务喂 Dify
- `sync_reports.py` —— 旧仓库 PDF 同步脚本（指向错误仓库，已失效）
- `news_fetcher.py` 中的 HTTP 服务模式（`NewsHandler` / 8766 端口）—— 仅 Dify 使用

若你确实仍在本机用 Dify 做备选生成，请另行保留上述文件的独立副本，不要合并回本仓库。
