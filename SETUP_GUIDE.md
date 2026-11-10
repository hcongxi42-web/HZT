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
- 无需 Chrome / 任何外部依赖（PDF 导出功能已移除，需要离线版请直接用浏览器打印报告页）

## 环境变量 / 仓库 Secrets

在 GitHub 仓库 `Settings → Secrets and variables → Actions` 中配置：

| 变量 | 必填 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek 模型调用密钥 |
| `DEEPSEEK_MODEL` | ⬜ | 覆盖默认模型（放在 **Variables**，不是 Secrets）。留空则用代码内默认值 |

> 本项目**没有**推送功能：报告通过 GitHub Pages 发布，需要通知请自行订阅 Pages 或另加脚本。

## 模型配置

默认模型在 `stock_report.py` 顶部的 `DEFAULT_DEEPSEEK_MODEL`（当前 `deepseek-flash`，
由 DeepSeek-V4.1-Flash 提供服务），展示名称由 `_MODEL_LABELS` 映射；换模型有两种方式：

1. **改代码**（会写进页面署名）：改 `DEFAULT_DEEPSEEK_MODEL`
2. **不改代码**：设置环境变量 `DEEPSEEK_MODEL`（本地）；或在仓库
   `Settings → Secrets and variables → Actions → Variables` 新建 `DEEPSEEK_MODEL`（CI）

> 旧 id `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 仍可调用，但对应模型已下线，
> 实际由 DeepSeek-V4.1-Flash 提供服务并按 Flash 价格计费；使用旧 id 时运行摘要会提示迁移。

每次运行的"体检单"里会显示本次实际使用的模型 id，便于确认是否切换成功。
若模型 id 写错，API 会返回 400，报告中对应板块会显示降级文案（不会中断流水线）。

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

- **自动触发**：北京时间 `07:17`（对应 UTC `23:17` 前一天），
  由 Python 内部判断交易日，非交易日跳过提交。
- **手动触发**：在 Actions 页面点击 `Run workflow`（workflow_dispatch）。
  只有早报一个场次，手动触发即出早报。
- **部署**：报告 HTML 提交回仓库后，自动上传并部署到 GitHub Pages。

> 2026-11 起**晚报场次已移除**：22:17 的定时任务、页面上的「早报/晚报」切换、
> 归档页的晚报条目一并下线，只保留早报。历史 `_pm.html` 文件仍在 `docs/` 里，
> 老链接可打开，只是不再生成、也不再进归档列表。

首次使用需在仓库 `Settings → Pages` 确认 Pages 源为 `GitHub Actions`。

## 输入：UP 主观点与信息差

将转录好的文本（`.txt`）放入 **`up主的每日观点/`** 目录（支持子目录），文件命名需含
日期与 UP 标识，例如：

```
up主的每日观点/7月6日_擒龙先生.12345678.ai-zh.txt
up主的每日观点/up小A/7月6号_观点.87654321.ai-zh.txt
```

- 程序按文件名中的 `X月X日 / X月X号` 解析日期，按 `.<数字>.ai-zh.txt` 解析 UP 标识。
- **早报**使用「昨日」观点（`date_offset=-1`），即昨天收盘后 UP 主说的话。
- 找不到匹配文件时，该模块自动降级（盘面分析照常，选股仅融合新闻与信息差）。

## 交易日与节假日

- **周末**：周六、周日都不生成（晚报移除后不再有"周末版"）。
- **法定节假日**：在仓库根目录 `holidays.json` 中登记**工作日休市日**（周六周日无需登记，程序自动跳过）：

  ```json
  { "holidays": ["2026-10-01", "2026-10-02"] }
  ```

  每年国务院公布放假安排后，补入农历假日（春节 / 清明 / 端午 / 中秋）。
- **只依赖节假日表**：早报在开盘前运行，行情天然是上一交易日，无法用「行情交易日≠今天」
  自证休市（原晚报的兜底判断已随晚报一起移除）。因此节假日完全以 `holidays.json` 为准；
  该文件缺失或损坏时不影响正常出报。

## 运行摘要

每次运行结束会在两处输出"体检单"：① 日志末尾；② GitHub Actions 运行页的 **Summary**。
逐项列出：运行场次、AI 模型、指数行情、各市场新闻条数、情绪硬指标、资金面、盘面分析、
UP主观点、信息差、AI选股、产出链接。
某节降级（如选股未发布草稿、观点降级展示原文、资金面无数据）在此一眼可见，不必翻日志。
被跳过的运行（周末 / 节假日 / 本场次报告已存在 / 未抓到新闻）同样会留痕并写明原因。

## 回归测试

```bash
python -m unittest discover -s tests -v
```

纯标准库，无需安装依赖；CI 在生成报告前会自动跑一遍（见 workflow 的 `Run regression tests`）。

## 输出说明

| 产物 | 位置 | 备注 |
|------|------|------|
| 最新报告 | `docs/index.html` | Pages 入口 |
| 当日跳转页 | `docs/report_YYYYMMDD.html` | <1KB 跳转桩，指向当天场次文件（老链接兼容） |
| 早报 | `docs/report_YYYYMMDD_am.html` | 唯一真实内容副本（后缀固定 `_am`，兼容历史链接与收藏） |
| Markdown | `report_YYYYMMDD_HHMM.md`（根目录） | 本地存档，`gitignore` |

## 版式模板（2026-11 起，后续改版以此为准）

页面结构固定为「顶部决策带 → 编号章节 → 页脚」，样式集中在 `docs/style.css` 末尾的
`版式层 v2` 区块，主程序的骨架生成在 `stock_report.py`。

| 构件 | 生成位置 | 样式类 |
|------|----------|--------|
| 顶部决策带（温度大字 + 一句话策略 + 体温说明） | `_render_verdict_band()` | `.verdict / .vside / .vtemp / .vmain / .vact / .vwhy` |
| 信号三格（最大共识 / 分歧 / 风险） | 同上 | `.signals / .sig` |
| 章节编号 01–10 | `markdown_to_html()` 内 `MAIN_SECTION_TITLES` 白名单 | `.sec-n`；未命中者渲染为 `.sec-h.sub` 小标题 |
| 目录（平条 + 编号） | `generate_html_report()` 的 `toc_items` | `.toc / .toc-link / .toc-n` |

**加新板块的做法**：在 `MAIN_SECTION_TITLES` 里按顺序插入标题 → 编号自动重排；
当天没有该节会自动跳过、不留空号。不要写死编号。

### 覆盖样式时的权重陷阱（踩过）

原样式表有四个选择器是**两级权重**，单类选择器覆盖不掉，必须同级或更高：

```css
.sec .sec-h        /* 原 .sec-h{color:var(--navy)} 在媒体查询里也有一份 */
.sec-hdr .sec-main /* 原规则 18px，单写 .sec-main 无效 */
.tbl tbody td      /* 原 .tbl tbody td{padding:9px 12px}，单写 .tbl td 无效 */
.toc .toc-link     /* 原为药丸样式，需两级才能改成平条 */
```

另外原版式的两个「签名」已在新版式中清零：`.sec` 的藏青左竖线、`.bi` 的灰蓝左竖线。
若要恢复竖线风格，改这两条即可，不必动其他规则。

## 已移除的旧组件（历史记录，避免混淆）



以下文件曾服务于 Dify + Docker 架构，现已删除，请勿找回：

- `cron_trigger.py`、`workflow_payload.json` —— Dify 工作流触发与定义
- `Dockerfile`、`docker-compose.yml` —— 仅用于把 news_fetcher 起 HTTP 服务喂 Dify
- `sync_reports.py` —— 旧仓库 PDF 同步脚本（指向错误仓库，已失效）
- `news_fetcher.py` 中的 HTTP 服务模式（`NewsHandler` / 8766 端口）—— 仅 Dify 使用

另：**PDF 导出功能已移除**（原 `generate_pdf()` 依赖 Chrome Headless，无任何页面对它建链，
且 CI 生成的 PDF 可能缺中文字体）。需要离线版时，用浏览器对报告页「打印 → 另存为 PDF」即可。

若你确实仍在本机用 Dify 做备选生成，请另行保留上述文件的独立副本，不要合并回本仓库。
