"""
独立版股市日报生成器 - 用于 GitHub Actions 定时运行
直接抓取新闻 + 调用通义千问 API 生成报告
支持微信推送（Server酱）+ GitHub Pages 详情页
"""

import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# 股票技术分析模块（可选依赖）
try:
    import stock_analyzer
except ImportError:
    stock_analyzer = None

# 共享爬取函数（避免与 news_fetcher.py 重复维护）
from news_fetcher import fetch_cls_news, fetch_eastmoney_news


# ============ 指数行情抓取 ============

def fetch_index_quotes():
    """从新浪财经抓取主要指数实时行情"""
    symbols = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
    }
    results = []
    for code, name in symbols.items():
        try:
            url = f"https://hq.sinajs.cn/list={code}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("gbk")
            parts = raw.split('"')[1].split(",")
            if code.startswith("sh") or code.startswith("sz"):
                price = float(parts[3])
                prev_close = float(parts[2])
                change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
                results.append({"name": name, "code": code, "price": f"{price:.2f}", "change": f"{change_pct:+.2f}%"})
            elif code.startswith("int_"):
                price = float(parts[1])
                change = float(parts[4]) if len(parts) > 4 else 0
                change_pct = float(parts[5].replace("%", "")) if len(parts) > 5 else 0
                results.append({"name": name, "code": code, "price": f"{price:.2f}", "change": f"{change_pct:+.2f}%"})
        except Exception:
            results.append({"name": name, "code": code, "price": "--", "change": "--"})
    return results


# ============ 新闻抓取部分 ============
# fetch_cls_news / fetch_eastmoney_news 统一从 news_fetcher.py 导入，避免重复维护


def fetch_all_news():
    """抓取 A 股新闻（财联社 + 东方财富）"""
    news = []
    news.extend(fetch_cls_news(market_filter="A"))
    news.extend(fetch_eastmoney_news())
    return news


def format_news(news_list):
    lines = [f"日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}", f"共抓取 {len(news_list)} 条新闻\n"]
    articles = [n for n in news_list if "error" not in n]
    if articles:
        lines.append(f"\n## A股 ({len(articles)}条)")
        for i, a in enumerate(articles[:15], 1):
            lines.append(f"{i}. [{a.get('time','')}] {a.get('title','')}")
            summary = a.get("summary", "")
            if summary and summary != a.get("title", ""):
                lines.append(f"   {summary[:120]}")
    return "\n".join(lines)


# ============ LLM 分析部分 ============

SYSTEM_PROMPT = "你是一位资深金融分析师，擅长解读股市新闻并生成专业的每日市场简报。你的分析客观、专业、简洁。"

USER_PROMPT_TEMPLATE = """请根据以下今日股市新闻，生成一份专业的《每日股市简报》。

要求：
1. **市场总览**：用2-3句话概括今日A股的整体动向
2. **重要新闻TOP5**：提取最重要的5条新闻，简要说明其影响
3. **个股聚焦**：如有值得关注的个股动态，列出并简析
4. **市场情绪**：基于新闻判断当前市场情绪（乐观/中性/悲观），说明理由
5. **明日展望**：基于今日消息面，简要预判明日可能走向

格式：Markdown，结构清晰，语言专业简洁。

---
今日新闻数据：

{news_text}"""


# ============ AI 选股部分 ============

STOCK_PICKER_SYSTEM_PROMPT = (
    "你是一位资深量化选股分析师，擅长从海量财经新闻中系统性挖掘具有短期或中期交易机会的个股。"
    "你需要全面扫描所有新闻，不遗漏任何被明确提及的股票。"
    "输出必须结构化、量化、可执行，对每只股票的判断必须有新闻依据，严禁编造。"
)

STOCK_PICKER_TEMPLATE = """请基于以下今日股市新闻，执行深度供应链选股分析。

任务要求：
1. **全面扫描**：仔细阅读每一条新闻，找出所有被明确提及的股票（包括股票名称、代码）。
2. **供应链推理**：对每条重大新闻中的核心公司，进一步推理其供应商、客户、竞争对手、产业链上下游是否也间接受益或受损。例如：
   - 若某机器人公司IPO利好 → 分析其减速器/伺服电机/传感器供应商
   - 若某芯片公司技术突破 → 分析其设备商、材料商、封测厂
   - 若某新能源车销量大增 → 分析其电池、电机、零部件供应商
3. **分类标记**：
   - 🔥 强势利好：业绩大增、重大合同、政策扶持、技术突破、并购重组
   - ⚡ 事件驱动：行业会议、产品发布、订单公告、获机构调研
   - ⚠️ 利空风险：业绩下滑、监管处罚、减持、安全事故、诉讼
4. **量化评分**（⭐1-5星）：
   - ⭐⭐⭐⭐⭐（5星）：核心龙头，直接受益，逻辑最硬
   - ⭐⭐⭐⭐（4星）：关联受益，供应链或竞争关系明确
   - ⭐⭐⭐（3星）：间接关联，受益程度一般
   - ⭐⭐（2星）：边缘关联，受益不确定
   - ⭐（1星）：仅概念沾边
5. **输出格式**：
   - 每条重大新闻作为二级标题（### 新闻标题）
   - 该新闻下用 Markdown 表格列出关联股票：股票名称代码 | 关联逻辑 | 利好类型 | 强度 | 周期
   - 所有 ⭐⭐⭐⭐⭐ 和 ⭐⭐⭐⭐ 的股票必须出现在表格中
   - 3星及以下股票在每条新闻最后只列名字和代码，不展开分析

注意：
- 不要编造没有新闻支撑的股票
- 同一只股票在不同新闻下可重复列出，但关联逻辑要不同
- 每条新闻至少推理出 2-3 只关联股票

---
今日新闻数据：

{news_text}"""


def call_stock_picker(news_text):
    """调用 LLM 执行系统性 AI 选股分析"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return ""
    url = "https://api.deepseek.com/v1/chat/completions"
    payload = json.dumps({
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": STOCK_PICKER_SYSTEM_PROMPT},
            {"role": "user", "content": STOCK_PICKER_TEMPLATE.format(news_text=news_text)},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]
    except Exception:
        return ""


def format_stock_picks(picks_md):
    """将选股结果格式化为日报独立板块"""
    if not picks_md or not picks_md.strip():
        return ""
    return (
        "\n\n---\n\n"
        "## 📌 每日精选个股（AI 选股）\n\n"
        f"{picks_md}\n"
    )


def call_llm(news_text):
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return "错误：未设置 DEEPSEEK_API_KEY 环境变量"
    url = "https://api.deepseek.com/v1/chat/completions"
    payload = json.dumps({
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(news_text=news_text)},
        ],
        "temperature": 0.5,
        "max_tokens": 4096,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return f"API 调用失败: HTTP {e.code} - {body[:300]}"
    except Exception as e:
        return f"API 调用失败: {str(e)}"


# ============ HTML 详情页生成 ============

def markdown_to_html(md):
    """将 Markdown 转为结构化 HTML（Bloomberg/WSJ 风格），支持表格。

    采用两阶段处理：
      1. 预扫描 — 将 Markdown 表格块转为 HTML，用占位符替换
      2. 标准处理 — 段落、标题、列表等，占位符直接插入原始 HTML
    """

    # ---- 阶段 1：提取表格 ----
    tables = []          # 存放生成的 HTML 表格
    lines = md.split("\n")
    processed_lines = []  # 替换占位符后的行序列
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if re.match(r"^\|.+\|$", stripped):
            # 检查下一行是否为分隔行 |---|:---:|...|
            if i + 1 < len(lines) and re.match(r"^\|(?:[\s\-:]+\|)+$", lines[i + 1].strip()):
                # ---------- 解析表头 ----------
                header_cells = [c.strip() for c in stripped.split("|")[1:-1]]
                sep_cells = [c.strip() for c in lines[i + 1].strip().split("|")[1:-1]]

                # 对齐方式
                aligns = []
                for sep in sep_cells:
                    if sep.startswith(":") and sep.endswith(":"):
                        aligns.append("center")
                    elif sep.endswith(":"):
                        aligns.append("right")
                    else:
                        aligns.append("left")
                while len(aligns) < len(header_cells):
                    aligns.append("left")

                # 构建表头 HTML
                html = '<div class="rpt-table-wrapper"><table class="rpt-table"><thead><tr>'
                for j, cell in enumerate(header_cells):
                    al = aligns[j]
                    html += f'<th style="text-align:{al}">{cell}</th>'
                html += "</tr></thead><tbody>"

                # ---------- 解析数据行 ----------
                i += 2  # 跳到表体第一行
                while i < len(lines) and re.match(r"^\|.+\|$", lines[i].strip()):
                    row_line = lines[i].strip()
                    cells = [c.strip() for c in row_line.split("|")[1:-1]]
                    html += "<tr>"
                    for j, cell in enumerate(cells):
                        al = aligns[j] if j < len(aligns) else "left"
                        html += f'<td style="text-align:{al}">{cell}</td>'
                    html += "</tr>"
                    i += 1

                html += "</tbody></table></div>"

                token = f"%%TABLE_{len(tables)}%%"
                tables.append(html)
                processed_lines.append(token)
                continue  # i 已被内层 while 推进到表尾之后

        processed_lines.append(raw)
        i += 1

    # ---- 阶段 2：标准解析（含表格占位符） ----
    md_clean = "\n".join(processed_lines)

    sections = []
    current_section = {"title": "", "content": []}

    for line in md_clean.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 表格占位符 → 直接注入 HTML
        tbl_match = re.match(r"^%%TABLE_(\d+)%%$", line)
        if tbl_match:
            idx = int(tbl_match.group(1))
            if idx < len(tables):
                current_section["content"].append(("raw_html", tables[idx]))
            continue

        h_match = re.match(r"^#{1,3}\s+(.+)$", line)
        if h_match:
            if current_section["title"] or current_section["content"]:
                sections.append(current_section)
            current_section = {"title": h_match.group(1), "content": []}
            continue

        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        ol_match = re.match(r"^(\d+)\.\s+(.+)$", line)
        if ol_match:
            current_section["content"].append(("ol", ol_match.group(1), ol_match.group(2)))
            continue
        ul_match = re.match(r"^[-*]\s+(.+)$", line)
        if ul_match:
            current_section["content"].append(("ul", ul_match.group(1)))
            continue
        if line == "---":
            continue
        current_section["content"].append(("p", line))

    if current_section["title"] or current_section["content"]:
        sections.append(current_section)

    html_parts = []
    for sec in sections:
        html_parts.append('<div class="rpt-section">')
        if sec["title"]:
            clean_title = re.sub(r"^[一二三四五六七八九十]+[、．.]?\s*", "", sec["title"])
            html_parts.append(f'<h3 class="rpt-heading">{clean_title}</h3>')
        for item in sec["content"]:
            typ = item[0]
            if typ == "ol":
                num, text = item[1], item[2]
                html_parts.append(f'<div class="rpt-news-item"><span class="rpt-num">{num}</span><div class="rpt-news-text">{text}</div></div>')
            elif typ == "ul":
                html_parts.append(f'<div class="rpt-bullet"><div class="rpt-bullet-text">{item[1]}</div></div>')
            elif typ == "raw_html":
                html_parts.append(item[1])  # 直接注入 HTML，不加任何包装
            else:
                html_parts.append(f'<p class="rpt-para">{item[1]}</p>')
        html_parts.append("</div>")

    return "\n".join(html_parts)


def generate_html_report(report, quotes, news_list, page_url="", page_base_url=""):
    """生成 Bloomberg/WSJ 风格 HTML 详情页"""
    today = datetime.now().strftime("%Y-%m-%d")
    today_en = datetime.now().strftime("%B %d, %Y")
    now_str = datetime.now().strftime("%H:%M")
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[datetime.now().weekday()]
    wk_cn = weekday_cn[datetime.now().weekday()]

    # 历史简报导航链接
    history_links_html = ""
    if page_base_url:
        for i in range(1, 6):
            d = datetime.now() - timedelta(days=i)
            d_str = d.strftime("%Y%m%d")
            label = d.strftime("%m月%d日")
            history_links_html += f'<a class="history-link" href="{page_base_url}report_{d_str}.html">{label}</a>'

    # 指数行情行
    quote_rows = ""
    for q in quotes:
        change_str = q["change"]
        is_up = change_str.startswith("+")
        is_down = change_str.startswith("-") and change_str != "--"
        cls = "up" if is_up else ("down" if is_down else "flat")
        arrow = "&#9650;" if is_up else ("&#9660;" if is_down else "")
        quote_rows += f'<div class="ticker {cls}"><div class="ticker-name">{q["name"]}</div><div class="ticker-price">{q["price"]}</div><div class="ticker-change">{arrow} {change_str}</div></div>'

    # 分时图
    chart_images = [
        ("上证指数", "sh000001", "https://image.sinajs.cn/newchart/min/n/sh000001.gif"),
        ("深证成指", "sz399001", "https://image.sinajs.cn/newchart/min/n/sz399001.gif"),
    ]
    chart_html = ""
    for name, code, img_url in chart_images:
        chart_html += f'<div class="chart-cell"><div class="chart-label">{name}</div><img src="{img_url}" alt="{name}" onerror="this.parentElement.style.display=\'none\'"></div>'

    # 新闻统计
    valid_news = [n for n in news_list if "error" not in n]
    a_count = len(valid_news)

    report_html = markdown_to_html(report)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MARKET BRIEF - {today}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700;900&family=Inter:wght@300;400;500;600;700&family=Noto+Serif+SC:wght@400;600;700;900&family=Noto+Sans+SC:wght@300;400;500;700&display=swap');

* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: 'Noto Sans SC', 'Inter', -apple-system, sans-serif;
  background: #f7f3ef; color: #1a1a1a; line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}}

/* ===== 顶部导航栏 (Bloomberg 风格) ===== */
.top-bar {{
  background: #111; color: #fff; padding: 0;
  border-bottom: 3px solid #c0392b;
}}
.top-bar-inner {{
  max-width: 860px; margin: 0 auto; padding: 12px 20px;
  display: flex; align-items: center; justify-content: space-between;
}}
.brand {{
  font-family: 'Playfair Display', 'Noto Serif SC', Georgia, serif;
  font-size: 22px; font-weight: 900; letter-spacing: 1px; color: #fff;
}}
.brand span {{ color: #c0392b; }}
.top-meta {{
  font-size: 11px; color: rgba(255,255,255,0.5); letter-spacing: 0.5px;
  text-align: right; line-height: 1.5;
}}

/* ===== Ticker 行情条 ===== */
.ticker-strip {{
  background: #1a1a1a; border-bottom: 1px solid #333;
  overflow-x: auto; white-space: nowrap; -webkit-overflow-scrolling: touch;
}}
.ticker-strip::-webkit-scrollbar {{ height: 0; }}
.ticker-inner {{
  max-width: 860px; margin: 0 auto; padding: 10px 20px;
  display: flex; gap: 0;
}}
.ticker {{
  flex: 0 0 auto; padding: 6px 16px; text-align: center;
  border-right: 1px solid #333; min-width: 110px;
}}
.ticker:last-child {{ border-right: none; }}
.ticker-name {{
  font-size: 10px; color: #888; letter-spacing: 1.5px;
  text-transform: uppercase; font-weight: 600; margin-bottom: 2px;
}}
.ticker-price {{
  font-family: 'Inter', monospace; font-size: 17px; font-weight: 700;
  color: #fff; font-variant-numeric: tabular-nums;
}}
.ticker-change {{
  font-family: 'Inter', monospace; font-size: 12px; font-weight: 600;
  margin-top: 1px;
}}
.ticker.up .ticker-change {{ color: #e74c3c; }}
.ticker.down .ticker-change {{ color: #27ae60; }}
.ticker.flat .ticker-change {{ color: #888; }}

/* ===== 主标题区 (WSJ 社论风格) ===== */
.masthead {{
  max-width: 860px; margin: 0 auto; padding: 36px 20px 24px;
  border-bottom: 2px solid #1a1a1a; text-align: center;
}}

/* ===== 历史简报导航条 ===== */
.history-nav {{
  max-width: 860px; margin: 0 auto; padding: 14px 20px;
  border-bottom: 1px solid #e0dcd5;
  display: flex; align-items: center; gap: 8px;
  flex-wrap: wrap; background: #faf8f5;
}}
.history-label {{
  font-size: 12px; font-weight: 700; color: #888;
  letter-spacing: 1px; text-transform: uppercase;
  margin-right: 6px;
}}
.history-link {{
  font-size: 12px; font-weight: 600; color: #c0392b;
  padding: 5px 12px; border-radius: 3px;
  background: #fff; border: 1px solid #e8e4dc;
  text-decoration: none; transition: all 0.2s;
}}
.history-link:hover {{
  background: #c0392b; color: #fff; border-color: #c0392b;
}}
.history-current {{
  background: #c0392b; color: #fff; border-color: #c0392b;
  cursor: default;
}}
.masthead-date {{
  font-size: 12px; color: #888; letter-spacing: 2px;
  text-transform: uppercase; font-weight: 500; margin-bottom: 10px;
}}
.masthead h1 {{
  font-family: 'Noto Serif SC', 'Playfair Display', Georgia, serif;
  font-size: 38px; font-weight: 900; color: #1a1a1a;
  letter-spacing: 2px; line-height: 1.3; margin-bottom: 10px;
}}
.masthead-sub {{
  font-size: 15px; color: #666; font-weight: 300;
  font-family: 'Noto Sans SC', 'Inter', sans-serif;
}}
.masthead-tags {{
  margin-top: 16px; display: flex; justify-content: center; gap: 10px;
  flex-wrap: wrap;
}}
.mtag {{
  font-size: 11px; font-weight: 600; letter-spacing: 1px;
  padding: 4px 14px; border-radius: 2px;
  text-transform: uppercase;
}}
.mtag-a {{ background: #fdecea; color: #c0392b; }}
.mtag-us {{ background: #eaf0fb; color: #2c5aa0; }}
.mtag-hk {{ background: #fef9e7; color: #b7950b; }}

/* ===== 内容容器 ===== */
.content {{ max-width: 860px; margin: 0 auto; padding: 0 20px 40px; }}

/* ===== 区块标题 ===== */
.sec-header {{
  font-family: 'Inter', 'Noto Sans SC', sans-serif;
  font-size: 11px; font-weight: 700; letter-spacing: 3px;
  text-transform: uppercase; color: #c0392b;
  padding: 20px 0 10px; margin-top: 28px;
  border-top: 3px solid #1a1a1a;
}}

/* ===== 走势图区 ===== */
.charts-row {{
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;
  margin-top: 16px;
}}
.chart-cell {{
  background: #fff; border: 1px solid #e0dcd5;
}}
.chart-label {{
  font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
  text-transform: uppercase; color: #666; text-align: center;
  padding: 10px 0 4px; border-bottom: 1px solid #f0ece6;
}}
.chart-cell img {{ width: 100%; display: block; }}

/* ===== AI 分析报告 ===== */
.report-body {{ margin-top: 16px; }}

.rpt-section {{ margin-bottom: 28px; }}
.rpt-section:last-child {{ margin-bottom: 0; }}

.rpt-heading {{
  font-family: 'Noto Serif SC', 'Playfair Display', Georgia, serif;
  font-size: 20px; font-weight: 700; color: #1a1a1a;
  padding-bottom: 8px; margin-bottom: 14px;
  border-bottom: 1px solid #d5d0c8;
}}

.rpt-news-item {{
  display: flex; align-items: flex-start; gap: 14px;
  padding: 14px 18px; margin: 10px 0;
  background: #fff; border: 1px solid #e8e4dc;
  border-left: 3px solid #c0392b;
  transition: box-shadow 0.2s;
}}
.rpt-news-item:hover {{ box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
.rpt-num {{
  flex-shrink: 0; width: 24px; height: 24px; line-height: 24px;
  text-align: center; font-family: 'Playfair Display', serif;
  font-size: 14px; font-weight: 900; color: #c0392b;
  border: 2px solid #c0392b; border-radius: 50%;
}}
.rpt-news-text {{
  font-size: 14.5px; line-height: 1.8; color: #2a2a2a;
}}
.rpt-news-text strong {{ color: #c0392b; font-weight: 700; }}

.rpt-bullet {{
  padding: 6px 18px 6px 32px; margin: 4px 0;
  position: relative;
}}
.rpt-bullet::before {{
  content: ''; position: absolute; left: 18px; top: 15px;
  width: 5px; height: 5px; background: #c0392b;
}}
.rpt-bullet-text {{
  font-size: 14.5px; line-height: 1.8; color: #2a2a2a;
}}
.rpt-bullet-text strong {{ color: #c0392b; }}

.rpt-para {{
  font-size: 15px; line-height: 1.9; color: #333;
  margin: 10px 0; text-align: justify;
}}
.rpt-para strong {{ color: #c0392b; }}

/* ===== 表格（AI 选股 / 数据）===== */
.rpt-table-wrapper {{
  overflow-x: auto; margin: 16px 0;
  -webkit-overflow-scrolling: touch;
}}
.rpt-table {{
  width: 100%; border-collapse: collapse;
  font-size: 13.5px; line-height: 1.7;
  background: #fff; border: 1px solid #e0dcd5;
}}
.rpt-table thead {{
  background: #1a1a1a; color: #fff;
}}
.rpt-table th {{
  padding: 10px 12px; font-size: 12px; font-weight: 600;
  letter-spacing: 0.5px; text-transform: uppercase;
  border-bottom: 2px solid #c0392b;
}}
.rpt-table td {{
  padding: 8px 12px; border-bottom: 1px solid #ece8e1;
  color: #2a2a2a;
}}
.rpt-table tbody tr:nth-child(even) {{
  background: #faf8f6;
}}
.rpt-table tbody tr:hover {{
  background: #fdf0ee;
}}
.rpt-table td strong {{ color: #c0392b; font-weight: 700; }}

/* ===== 页脚 ===== */
.site-footer {{
  max-width: 860px; margin: 0 auto; padding: 30px 20px 50px;
  border-top: 3px solid #1a1a1a; text-align: center;
}}
.footer-brand {{
  font-family: 'Playfair Display', 'Noto Serif SC', serif;
  font-size: 15px; font-weight: 700; color: #1a1a1a;
  letter-spacing: 1px; margin-bottom: 8px;
}}
.footer-info {{
  font-size: 11px; color: #999; line-height: 2; letter-spacing: 0.3px;
}}
.footer-disclaimer {{
  font-size: 10px; color: #bbb; margin-top: 12px;
  padding-top: 12px; border-top: 1px solid #e0dcd5;
  line-height: 1.8;
}}
.online-link {{
  display: inline-block; margin-top: 14px;
  padding: 10px 28px; background: #c0392b; color: #fff;
  font-size: 13px; font-weight: 600; letter-spacing: 1px;
  text-decoration: none; border-radius: 3px;
  transition: background 0.2s;
}}
.online-link:hover {{ background: #a93226; }}

/* ===== 响应式 ===== */
@media (max-width: 600px) {{
  .masthead h1 {{ font-size: 28px; }}
  .top-bar-inner {{ flex-direction: column; gap: 4px; text-align: center; }}
  .top-meta {{ text-align: center; }}
  .ticker-inner {{ padding: 8px 12px; }}
  .ticker {{ min-width: 90px; padding: 6px 10px; }}
  .ticker-price {{ font-size: 15px; }}
  .charts-row {{ grid-template-columns: 1fr; }}
  .content {{ padding: 0 14px 30px; }}
  .rpt-heading {{ font-size: 18px; }}
  .rpt-news-item {{ padding: 10px 12px; }}
  .masthead {{ padding: 24px 14px 18px; }}
}}
</style>
</head>
<body>

<!-- 顶部导航 -->
<div class="top-bar">
  <div class="top-bar-inner">
    <div class="brand">MARKET<span>BRIEF</span></div>
    <div class="top-meta">{weekday}, {today_en}<br>{now_str} CST</div>
  </div>
</div>

<!-- 行情条 -->
<div class="ticker-strip">
  <div class="ticker-inner">{quote_rows}</div>
</div>

<!-- 历史简报导航 -->
<div class="history-nav">
  <span class="history-label">📅 历史简报</span>
  {history_links_html}
</div>

<!-- 标题区 -->
<div class="masthead">
  <div class="masthead-date">{today} {wk_cn}</div>
  <h1>每日股市简报</h1>
  <div class="masthead-sub">AI-Powered Daily Market Intelligence</div>
  <div class="masthead-tags">
    <span class="mtag mtag-a">A股 {a_count} 条</span>
  </div>
</div>

<div class="content">

  <!-- 走势图 -->
  <div class="sec-header">INTRADAY CHARTS</div>
  <div class="charts-row">{chart_html}</div>

  <!-- AI 报告 -->
  <div class="sec-header">MARKET ANALYSIS</div>
  <div class="report-body">{report_html}</div>

</div>

<div class="site-footer">
  <div class="footer-brand">MARKET BRIEF</div>
  {f'<a class="online-link" href="{page_url}">查看在线版本</a>' if page_url else ''}
  <div class="footer-info">
    Data: CLS / East Money / Sina Finance<br>
    AI Analysis by Tongyi Qwen
  </div>
  <div class="footer-disclaimer">
    本报告由 AI 自动生成，仅供参考，不构成任何投资建议。<br>
    市场有风险，投资需谨慎。
  </div>
</div>

</body>
</html>"""
    return html


# ============ PDF 生成 ============

def generate_pdf(html_path):
    """将 HTML 报告转为 PDF（使用 Chrome Headless）"""
    import subprocess
    import shutil

    now = datetime.now()
    pdf_filename = f"股市简报_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    pdf_dir = os.path.join("docs", "pdf")
    os.makedirs(pdf_dir, exist_ok=True)
    pdf_path = os.path.join(pdf_dir, pdf_filename)

    chrome_candidates = [
        "google-chrome-stable", "google-chrome", "chromium-browser", "chromium",
        "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    chrome = None
    for c in chrome_candidates:
        if shutil.which(c) or os.path.exists(c):
            chrome = c
            break

    if not chrome:
        print("未找到 Chrome，跳过 PDF 生成")
        return None

    abs_html = os.path.abspath(html_path)
    try:
        subprocess.run([
            chrome, "--headless", "--disable-gpu", "--no-sandbox",
            "--disable-software-rasterizer",
            f"--print-to-pdf={pdf_path}",
            "--no-pdf-header-footer",
            f"file://{abs_html}"
        ], capture_output=True, timeout=30)
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            shutil.copy2(pdf_path, os.path.join(pdf_dir, "latest.pdf"))
            print(f"PDF 已生成: {pdf_path}")
            return pdf_path
        else:
            print("PDF 生成失败: 文件为空或不存在")
            return None
    except Exception as e:
        print(f"PDF 生成失败: {e}")
        return None


# ============ GitHub Pages 部署 ============

def deploy_github_pages(html_content):
    """将 HTML 报告写入 docs/ 目录供 GitHub Pages 使用"""
    today = datetime.now().strftime("%Y%m%d")
    os.makedirs("docs", exist_ok=True)

    # 写入当天报告
    report_path = f"docs/report_{today}.html"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    # 写入 index.html（始终指向最新报告）
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html_content)

    page_url = os.environ.get("GITHUB_PAGES_URL", "")
    if page_url:
        full_url = f"{page_url}/report_{today}.html"
    else:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if repo:
            owner = repo.split("/")[0].lower()
            repo_name = repo.split("/")[1]
            full_url = f"https://{owner}.github.io/{repo_name}/report_{today}.html"
        else:
            full_url = ""

    print(f"详情页已生成: {report_path}")
    if full_url:
        print(f"GitHub Pages URL: {full_url}")
    return full_url


# ============ Server酱微信推送（公众号风格）============

def send_wechat(report, quotes, page_url):
    """通过 Server酱 推送精美的微信消息"""
    send_key = os.environ.get("SERVERCHAN_KEY", "")
    if not send_key:
        print("未设置 SERVERCHAN_KEY，跳过微信推送")
        return

    today = datetime.now().strftime("%Y年%m月%d日")
    title = f"📈 每日股市简报 | {today}"

    # 构建公众号风格的 Markdown 内容
    lines = []

    # 行情概览表格
    lines.append("## 📊 今日行情一览\n")
    lines.append("| 指数 | 最新价 | 涨跌幅 |")
    lines.append("|:---:|:---:|:---:|")
    for q in quotes:
        change = q["change"]
        if change.startswith("+"):
            emoji = "🔴"
        elif change.startswith("-") and change != "--":
            emoji = "🟢"
        else:
            emoji = "⚪"
        lines.append(f"| {q['name']} | {q['price']} | {emoji} {change} |")
    lines.append("")

    # 大盘分时走势图
    lines.append("## 📈 大盘走势\n")
    lines.append("![上证指数](https://image.sinajs.cn/newchart/min/n/sh000001.gif)")
    lines.append("")

    # 分隔线
    lines.append("---\n")

    # AI 分析报告
    lines.append("## 🤖 AI 分析报告\n")
    lines.append(report)
    lines.append("")

    # 详情链接
    lines.append("---\n")
    if page_url:
        lines.append(f"### 🔗 [点击查看完整图文报告]({page_url})\n")
    lines.append(f"> 📅 {today} · 数据来自财联社/东方财富/新浪财经 · AI分析仅供参考")

    desp = "\n".join(lines)

    url = f"https://sctapi.ftqq.com/{send_key}.send"
    payload = urllib.parse.urlencode({
        "title": title,
        "desp": desp,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if data.get("code") == 0:
            print("微信推送成功")
        else:
            print(f"微信推送失败: {data.get('message', '')}")
    except Exception as e:
        print(f"微信推送失败: {e}")


# ============ Webhook 推送 ============

def send_webhook(text):
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    if not webhook_url:
        return
    if "qyapi.weixin" in webhook_url:
        payload = json.dumps({"msgtype": "markdown", "markdown": {"content": text[:4096]}})
    elif "dingtalk" in webhook_url:
        payload = json.dumps({"msgtype": "markdown", "markdown": {"title": "股市日报", "text": text[:4096]}})
    elif "feishu" in webhook_url or "larksuite" in webhook_url:
        payload = json.dumps({"msg_type": "text", "content": {"text": text[:4096]}})
    else:
        payload = json.dumps({"text": text[:4096]})
    req = urllib.request.Request(webhook_url, data=payload.encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Webhook 推送成功: {resp.status}")
    except Exception as e:
        print(f"Webhook 推送失败: {e}")


# ============ 主流程 ============

def main():
    print(f"[{datetime.now()}] 开始生成股市日报...")

    # 1. 抓取指数行情
    print("正在抓取指数行情...")
    quotes = fetch_index_quotes()
    for q in quotes:
        print(f"  {q['name']}: {q['price']} ({q['change']})")

    # 2. 抓取新闻
    print("正在抓取新闻...")
    news = fetch_all_news()
    valid = [n for n in news if "error" not in n]
    errors = [n for n in news if "error" in n]
    print(f"  抓取完成: {len(valid)} 条新闻, {len(errors)} 个错误")

    if not valid:
        print("没有抓取到任何新闻，退出")
        return

    # 3. 格式化 & 调用 LLM
    news_text = format_news(news)
    print("正在生成分析报告...")
    report = call_llm(news_text)

    # 3.5 AI 选股分析
    print("正在执行 AI 选股...")
    stock_picks = call_stock_picker(news_text)

    # 预先构造 GitHub Pages 基础 URL（用于图表嵌入）
    today_str = datetime.now().strftime("%Y%m%d")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        owner = repo.split("/")[0].lower()
        repo_name = repo.split("/")[1]
        page_base_url = f"https://{owner}.github.io/{repo_name}/"
    else:
        page_base_url = "https://ivyxiashengjie.github.io/stock-daily-report/"

    # 对 5-4 星股票生成技术分析图表
    if stock_picks and stock_analyzer:
        print("  正在解析高评分股票...")
        stock_list = stock_analyzer.parse_stock_picks(stock_picks)
        if stock_list:
            print(f"  发现 {len(stock_list)} 只高评分股票，正在生成技术分析图...")
            charts_dir = os.path.join("docs", "charts")
            chart_urls = stock_analyzer.analyze_stocks(stock_list, charts_dir, page_base_url)
            if chart_urls:
                # 在 stock_picks 末尾追加图表链接
                chart_md = "\n\n### 📊 技术分析图\n\n"
                for name, code, stars, url in chart_urls:
                    chart_md += f"**{name} {code}**（{'⭐' * stars}）\n\n"
                    chart_md += f"![{name} 技术分析]({url})\n\n"
                stock_picks += chart_md
                print(f"  已生成 {len(chart_urls)} 张技术分析图")
            else:
                print("  未能生成任何图表")
        else:
            print("  未解析到高评分股票（可能是代码格式不匹配）")
    elif not stock_analyzer:
        print("  stock_analyzer 模块未加载")

    if stock_picks:
        report += format_stock_picks(stock_picks)
        print("AI 选股完成")

    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)

    # 4. 生成 HTML 详情页
    print("\n正在生成详情页...")
    page_url = f"{page_base_url}report_{today_str}.html"
    html = generate_html_report(report, quotes, news, page_url, page_base_url)
    page_url = deploy_github_pages(html)

    # 4.5 生成 PDF
    print("正在生成 PDF...")
    html_file = f"docs/report_{datetime.now().strftime('%Y%m%d')}.html"
    generate_pdf(html_file)

    # 5. 保存 Markdown
    report_file = f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(f"# 每日股市简报 - {datetime.now().strftime('%Y-%m-%d')}\n\n")
        f.write(report)
    print(f"Markdown 报告: {report_file}")

    # 6. GitHub Actions 输出
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"report_file={report_file}\n")
            f.write(f"page_url={page_url}\n")

    # 7. 推送微信
    send_wechat(report, quotes, page_url)

    # 8. 推送 Webhook
    send_webhook(report)

    print(f"\n[{datetime.now()}] 完成")


if __name__ == "__main__":
    main()
