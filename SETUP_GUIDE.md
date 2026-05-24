# 股市新闻 Agent 搭建指南
news_fetcher.py
新闻抓取 HTTP 服务。从财联社、东方财富、新浪财经等多个金融网站实时抓取 A股/美股/港股新闻，并提供 REST API（端口 8766）供 Dify Workflow 调用

stock_report.py
独立版报告生成器。用于 GitHub Actions 定时运行，集成了：新闻抓取 → LLM 分析 → HTML/PDF 生成 → 微信推送（Server 酱）→ GitHub Pages 部署

sync_reports.py
PDF 报告同步工具。从 GitHub 仓库下载生成的 PDF 报告到本地 ~/StockReports/ 文件夹，用于本地备份和归档

cron_trigger.py

Dockerfile
Docker 镜像构建文件，用于将 news_fetcher.py 打包成容器镜像
docker-compose.yml
Docker Compose 配置，用于快速启动 Dify 和新闻服务容器

workflow_payload.json
GitHub 工作流的 payload 配置文件


## 架构总览

```
[Cron 定时器] --触发--> [Dify Workflow API]
                              |
                    +---------+---------+
                    |                   |
              [HTTP Request]      [HTTP Request]
              (A股新闻)           (美股/港股新闻)
                    |                   |
                    +----->  [合并]  <---+
                              |
                         [LLM 分析]
                     (摘要+情绪+个股)
                              |
                        [输出报告]
```
每日定时 → GitHub Actions/Cron → news_fetcher.py(爬新闻)
                                ↓
                        stock_report.py/Dify(LLM分析)
                                ↓
                    生成 HTML/PDF/MD 报告
                                ↓
          推送到：微信 | GitHub Pages | 本地文件
                                ↓
                        sync_reports.py(同步备份)
## 第一步：配置 LLM 模型

1. 打开 http://localhost 进入 Dify
2. 首次登录需要设置管理员账号
3. 点击右上角头像 → **设置** → **模型供应商**
4. 选择国产模型供应商（推荐以下任一）：
   - **DeepSeek** - 便宜好用，填入 API Key 即可
   - **通义千问（阿里云）** - 填入 DashScope API Key
   - **智谱 GLM** - 填入 API Key
5. 添加好后，回到设置 → **模型供应商** → **系统模型设置**，选一个默认模型

## 第二步：创建 Workflow 应用

1. 点击左上角 **工作室** → **创建应用** → **从空白创建**
2. 选择类型：**工作流 (Workflow)**
3. 名称填：`股市日报Agent`

## 第三步：搭建工作流节点

进入工作流编辑器后，按以下顺序添加节点：

### 3.1 开始节点（已自带）
- 添加输入变量：
  - `date` (文本) - 日期
  - `market` (文本) - 市场，默认值 `all`

### 3.2 添加 HTTP Request 节点 - 抓取新闻
- 点击 `+` 添加节点 → 选择 **HTTP 请求**
- 名称：`抓取股市新闻`
- 方法：`GET`
- URL：`http://host.docker.internal:8766/news?market={{#1711111111111.market#}}`
  （`host.docker.internal` 是从 Docker 容器内访问宿主机的地址）

  > 如果上面的地址不通，尝试用 `http://stock-news-fetcher:8766/news?market={{#1711111111111.market#}}`
  > （因为新闻服务已加入 Dify 的 Docker 网络）

- 超时：30 秒

### 3.3 添加 Code 节点 - 整理新闻数据
- 点击 `+` → **代码执行**
- 名称：`整理新闻`
- 语言：Python3
- 输入变量：`raw_response` = HTTP 请求节点的 body
- 代码：

```python
import json

def main(raw_response: str) -> dict:
    try:
        data = json.loads(raw_response)
    except:
        return {"result": "新闻数据解析失败", "count": "0"}

    news_list = data.get("news", [])
    date = data.get("date", "")

    # 按市场分组
    markets = {"A股": [], "美股": [], "港股": []}
    for item in news_list:
        if "error" in item:
            continue
        market = item.get("market", "其他")
        if market in markets:
            markets[market].append(item)

    # 格式化输出
    lines = [f"日期: {date}", f"共抓取 {len(news_list)} 条新闻\n"]

    for market_name, articles in markets.items():
        if not articles:
            continue
        lines.append(f"\n## {market_name} ({len(articles)}条)")
        for i, a in enumerate(articles[:15], 1):
            lines.append(f"{i}. [{a.get('time','')}] {a.get('title','')}")
            summary = a.get('summary', '')
            if summary and summary != a.get('title', ''):
                lines.append(f"   {summary[:100]}")

    result = "\n".join(lines)
    count = str(len(news_list))
    return {"result": result, "count": count}
```

- 输出变量：`result` (String), `count` (String)

### 3.4 添加 LLM 节点 - 生成分析报告
- 点击 `+` → **LLM**
- 名称：`生成股市日报`
- 选择你配置好的模型
- System Prompt：

```
你是一位资深金融分析师，擅长解读股市新闻并生成专业的每日市场简报。
```

- User Prompt：

```
请根据以下今日股市新闻，生成一份专业的《每日股市简报》。

要求：
1. **市场总览**：用 2-3 句话概括今日 A股、美股、港股的整体表现
2. **重要新闻摘要**：提取最重要的 5-8 条新闻，按重要程度排列
3. **个股聚焦**：如果有值得关注的个股动态，列出来并简析
4. **市场情绪分析**：基于新闻内容判断当前市场情绪（乐观/中性/悲观），并说明理由
5. **明日展望**：基于今日消息面，简要预判明日可能的市场走向

格式要求：使用 Markdown 格式，结构清晰，语言专业简洁。

---
今日新闻数据：

{{#code_node_id.result#}}
```

### 3.5 添加结束节点
- 点击 `+` → **结束**
- 输出变量：
  - `report` = LLM 节点的输出文本

### 3.6 连接所有节点
确保连线顺序：
```
开始 → HTTP请求(抓取新闻) → 代码(整理新闻) → LLM(生成日报) → 结束
```

## 第四步：测试运行

1. 点击右上角 **运行** 按钮
2. 输入 date = `2026-02-27`, market = `all`
3. 查看输出的股市日报是否正常

## 第五步：发布并获取 API Key

1. 点击右上角 **发布**
2. 发布后，点击左侧 **访问 API**
3. 在 API Keys 区域，点击 **创建 API Key**
4. 复制 API Key（格式如 `app-xxxxxxxxxxxx`）

## 第六步：配置定时触发

编辑 cron 触发脚本：

```bash
vim ~/dify-stock-agent/cron_trigger.py
```

将 `DIFY_API_KEY` 替换为第五步获取的 Key。

如需推送到企业微信/钉钉/飞书，填入对应的 Webhook URL。

### 设置 Cron 定时任务

```bash
# 编辑 crontab
crontab -e

# 添加以下行（每天早上 8:30 和下午 15:30 各运行一次）
30 8 * * 1-5 /usr/bin/python3 /Users/shengjiexia/dify-stock-agent/cron_trigger.py >> /Users/shengjiexia/dify-stock-agent/cron.log 2>&1
30 15 * * 1-5 /usr/bin/python3 /Users/shengjiexia/dify-stock-agent/cron_trigger.py >> /Users/shengjiexia/dify-stock-agent/cron.log 2>&1
```

说明：`1-5` 表示周一到周五（交易日）

## 文件说明

| 文件 | 用途 |
|------|------|
| `news_fetcher.py` | 新闻抓取服务（HTTP API，端口 8766） |
| `cron_trigger.py` | 定时触发脚本（调用 Dify API + Webhook 推送） |
| `docker-compose.yml` | 新闻服务容器配置 |
| `Dockerfile` | 新闻服务镜像构建 |

## 常用命令

```bash
# 手动测试新闻抓取
python3 ~/dify-stock-agent/news_fetcher.py test

# 重启新闻服务
cd ~/dify-stock-agent && docker compose restart

# 手动触发一次 Workflow
python3 ~/dify-stock-agent/cron_trigger.py

# 查看 cron 执行日志
tail -f ~/dify-stock-agent/cron.log

# 查看 Dify 服务状态
cd ~/dify/docker && docker compose ps
```
