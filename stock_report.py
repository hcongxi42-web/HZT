"""
独立版股市日报生成器 - 用于 GitHub Actions 定时运行
多市场新闻聚合 + DeepSeek AI 分析 + 技术图表 + GitHub Pages 部署
支持微信推送（Server酱）+ 多平台 Webhook
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

# 共享新闻 & 资金面抓取模块
from news_fetcher import fetch_all_news_flat


# ============================================================
#  指数行情抓取
# ============================================================

def fetch_index_quotes():
    """从新浪财经抓取 A 股主要指数 + 恒生 + 纳斯达克 实时行情。"""
    symbols = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
        "int_hangseng": "恒生指数",
        "int_nasdaq": "纳斯达克",
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
                high = float(parts[4])
                low = float(parts[5])
                change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
                results.append({
                    "name": name, "code": code,
                    "price": f"{price:.2f}",
                    "change": f"{change_pct:+.2f}%",
                    "high": f"{high:.2f}", "low": f"{low:.2f}",
                })
            elif code.startswith("int_"):
                price = float(parts[1])
                change_pct = float(parts[5].replace("%", "")) if len(parts) > 5 else 0
                results.append({
                    "name": name, "code": code,
                    "price": f"{price:.2f}",
                    "change": f"{change_pct:+.2f}%",
                    "high": "--", "low": "--",
                })
        except Exception:
            results.append({"name": name, "code": code, "price": "--", "change": "--", "high": "--", "low": "--"})
    return results


# ============================================================
#  新闻抓取 & 格式化
# ============================================================

def fetch_all_news():
    """抓取全市场新闻 + 资金面数据（已通过 news_fetcher 聚合）。"""
    articles, errors, northbound, fund_flow = fetch_all_news_flat("all")
    for err in errors:
        print(f"  [WARN] {err.get('error', str(err))}")
    return articles, northbound, fund_flow


def format_news(news_list, northbound=None, fund_flow=None):
    """将多市场新闻和资金面数据格式化为 LLM 可读文本。"""
    today_str = datetime.now().strftime("%Y-%m-%d")

    lines = [
        f"日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"今日 {today_str} 是交易日，以下为当日多市场资讯汇总。",
        "",
    ]

    # ---- 资金面概览 ----
    if fund_flow:
        lines.append("## 资金面 · 大盘主力资金流向（近5日）")
        lines.append("")
        for row in fund_flow:
            direction = "净流入" if row["net_flow"] >= 0 else "净流出"
            lines.append(
                f"- {row['date']}  主力{direction} {abs(row['net_flow']):.2f} 亿元  "
                f"(流入 {row['main_in']:.2f} 亿 / 流出 {row['main_out']:.2f} 亿)"
            )
        lines.append("")

    if northbound:
        lines.append("## 资金面 · 北向资金（近5日）")
        lines.append("")
        for row in northbound:
            direction = "净流入" if row.get("net_buy_total", 0) >= 0 else "净流出"
            lines.append(
                f"- {row['date']}  北向{direction} {abs(row['net_buy_total']):.2f} 亿元  "
                f"(沪股通 {row['net_buy_sh']:+.2f} 亿 / 深股通 {row['net_buy_sz']:+.2f} 亿)"
            )
        lines.append("")

    # ---- 新闻正文 ----
    lines.append(f"共抓取 {len(news_list)} 条新闻")
    lines.append("")

    # 按市场分组
    markets = {"A股": [], "美股": [], "港股": []}
    for a in news_list:
        mkt = a.get("market", "其他")
        if mkt in markets:
            markets[mkt].append(a)
        else:
            markets.setdefault("其他", []).append(a)

    for mkt_name, articles in markets.items():
        if not articles:
            continue
        lines.append(f"\n## {mkt_name} ({len(articles)}条)")
        # 按来源分组
        sources = {}
        for a in articles:
            src = a.get("source", "?")
            sources.setdefault(src, []).append(a)

        for i, a in enumerate(articles[:30], 1):
            title = a.get("title", "")
            summary = a.get("summary", "")
            time_str = a.get("time", "")
            src = a.get("source", "")
            news_type = a.get("type", "")

            line = f"{i}. [{time_str}] [{src}] {title}"
            lines.append(line)
            # 摘要（如果与标题不同且有内容）
            if summary and summary != title and len(summary) > 10:
                lines.append(f"   {summary[:200]}")

    return "\n".join(lines)


def format_news_brief(news_list):
    """轻量版新闻摘要——用于微信推送等短场景。"""
    lines = [f">>> {datetime.now().strftime('%Y-%m-%d')} 多市场资讯 <<<"]
    markets = {"A股": [], "美股": [], "港股": []}
    for a in news_list:
        mkt = a.get("market", "")
        if mkt in markets:
            markets[mkt].append(a)

    for mkt, articles in markets.items():
        if articles:
            lines.append(f"\n【{mkt} TOP 8】")
            for i, a in enumerate(articles[:8], 1):
                lines.append(f"{i}. {a.get('title','')[:60]}")
    return "\n".join(lines)


# ============================================================
#  LLM 分析 — 提示词
# ============================================================

SYSTEM_PROMPT = (
    "你是一位顶级对冲基金策略分析师，拥有 20 年全球宏观与多资产配置经验。"
    "你擅长从海量信息中提炼关键信号，用简洁犀利的语言呈现市场判断。"
    "你的读者是专业投资者，不需要科普，需要的是洞察和可操作的观点。"
)

USER_PROMPT_TEMPLATE = """请基于以下今日多市场资讯和资金面数据，生成一份机构级《每日市场情报》。

要求：
1. **资金面信号**：基于主力资金流向和北向资金数据，判断当前资金态度（进攻/防守/观望），1-2句话点出关键信号
2. **A股主线扫描**：识别今日 A 股最核心的 3 条主线，每条说明驱动逻辑和可持续性判断
3. **跨市场联动**：美股、港股的异动对 A 股可能产生的映射和传导
4. **重要舆情 TOP 8**：提取最重要的 8 条新闻，简述影响，标注利好/利空/中性
5. **情绪温度计**：综合资金面+新闻面，给出市场情绪评级（🔥热/😊偏暖/😐中性/😟谨慎/❄️冰点），说明理由
6. **明日推演**：基于今日盘面和消息面，推演明日最可能的 2-3 个情景

风格要求：
- 语言犀利直接，避免废话
- 每部分 3-5 句即可，不要长篇大论
- 用「」标记关键术语和股票名称

---
数据：

{news_text}"""


# ============================================================
#  AI 选股 — 提示词
# ============================================================

STOCK_PICKER_SYSTEM_PROMPT = (
    "你是一位以产业链逻辑见长的量化选股专家，"
    "擅长从新闻事件中推导出整个产业链的受益/受损传导链，"
    "精准锁定具有交易价值的个股。"
    "你对每只股票的判断必须有清晰的新闻依据或产业链逻辑，绝不编造。"
)

STOCK_PICKER_TEMPLATE = """请基于以下今日多市场资讯和资金面数据，执行系统性选股分析。

步骤：

1. **新闻扫描**：逐条扫描所有新闻，找出被明确提及的所有股票
2. **产业链扩展**：对每条重大新闻，推导其上游供应商、下游客户、竞争对标、产业链替代标的
   - 例：「某操作系统获政策支持」→ 核心软件商 → 适配芯片商 → 服务器/PC 整机商 → 行业应用商
   - 例：「某公司签署大单」→ 该公司上游设备/材料商 → 同行业竞争对手（分流）
3. **资金面过滤**：结合主力资金流向，优先关注资金持续流入的板块/方向
4. **分级标注**：
   - 🔥🔥🔥 核心标的：直接受益/受损，逻辑清晰，短期可见催化
   - 🔥🔥 关联标的：产业链传导，间接受益
   - 🔥 观察标的：概念沾边，逻辑较长
5. **输出结构**：
   - 每条重大新闻作为 ### 三级标题
   - 该新闻后用表格列出关联股票：| 股票名称代码 | 关联逻辑 | 方向 | 确定性 |
   - 🔥🔥🔥（核心）和 🔥🔥（关联）必须放入表格
   - 🔥（观察）股票在每条新闻末尾列表形式简要提及
6. **避雷区**：用 ### ⚠️ 避雷 列出今日出现明确利空的个股（减持/业绩暴雷/监管处罚/安全事故）

注意：
- 不要编造新闻中不存在的股票
- 每个表格至少要有 3-5 行（显示出产业链推理深度）
- 如果某条新闻极其重要，可以单独用一整个 ### 板块来深度展开

---
数据：

{news_text}"""


# ============================================================
#  LLM 调用
# ============================================================

def _call_deepseek(system_prompt, user_prompt, temperature=0.5, max_tokens=4096):
    """通用 DeepSeek API 调用。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return "错误：未设置 DEEPSEEK_API_KEY 环境变量"

    url = "https://api.deepseek.com/v1/chat/completions"
    payload = json.dumps({
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return f"API 调用失败: HTTP {e.code} - {body[:300]}"
    except Exception as e:
        return f"API 调用失败: {str(e)}"


def call_llm(news_text):
    """调用 LLM 生成市场分析报告。"""
    return _call_deepseek(SYSTEM_PROMPT, USER_PROMPT_TEMPLATE.format(news_text=news_text),
                          temperature=0.5, max_tokens=4096)


def call_stock_picker(news_text):
    """调用 LLM 执行产业链选股分析。"""
    return _call_deepseek(STOCK_PICKER_SYSTEM_PROMPT, STOCK_PICKER_TEMPLATE.format(news_text=news_text),
                          temperature=0.3, max_tokens=6144)


def format_stock_picks(picks_md):
    """将选股结果封装为日报板块。"""
    if not picks_md or not picks_md.strip():
        return ""
    return (
        "\n\n---\n\n"
        "## 📌 产业链选股（AI 驱动）\n\n"
        f"{picks_md}\n"
    )


# ============================================================
#  图表嵌入
# ============================================================

def insert_charts_into_picks(stock_picks, chart_urls):
    """将技术分析图精准插入到对应股票所在的 section 下方。

    策略：
      1. 将选股文本按 ### 标题拆分为多个 section
      2. 每个 section 独立处理：扫描其中的股票代码 → 匹配图表 → 插入到该 section 的表格之后
      3. 每张图只插入一次（优先插在第一个提到该股票代码的 section）
      4. 未匹配的图表追加在末尾
      5. 移除任何残留的独立「技术分析图」板块
    """
    if not chart_urls:
        return stock_picks

    # 移除旧版独立「技术分析图」板块（兼容历史数据）
    stock_picks = re.sub(
        r'\n*###\s*📊\s*技术分析图\s*\n(?:\*\*.*?\*\*[^\n]*\n|!\[.*?\]\(.*?\)\n|\n)*',
        '', stock_picks
    )

    # 构建代码 → 图表 URL 的映射
    code_to_chart = {}
    for name, code, _stars, url in chart_urls:
        num_match = re.search(r'(\d{6})', code)
        if num_match:
            code6 = num_match.group(1)
            if code6 not in code_to_chart:
                code_to_chart[code6] = (name, code, url)

    placed_codes = set()

    # ---- 按 ### 标题拆分 section ----
    # 使用正则可以保留分隔符
    section_pattern = re.compile(r'^(?=###\s)', re.MULTILINE)
    raw_sections = section_pattern.split(stock_picks)

    if not raw_sections:
        return stock_picks

    # 第一个分段可能是 preamble（### 之前的内容），保留它
    processed_sections = []
    for sec_text in raw_sections:
        if not sec_text.strip():
            processed_sections.append(sec_text)
            continue

        # 如果是 preamble（不以 ### 开头），不做图表插入
        if not re.match(r'^###\s', sec_text.strip()):
            processed_sections.append(sec_text)
            continue

        # ---- 找到这个 section 里的所有股票代码 ----
        codes_in_section = set()
        for m in re.finditer(r'(\d{6})', sec_text):
            codes_in_section.add(m.group(1))

        # ---- 找到 section 中表格的结束位置 ----
        lines = sec_text.split('\n')
        in_table = False
        table_last_line = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            is_table_row = bool(re.match(r'^\|.+\|$', stripped))
            if is_table_row and not in_table:
                in_table = True
                table_last_line = i
            elif is_table_row and in_table:
                table_last_line = i
            elif not is_table_row and in_table:
                break  # 表格结束

        # ---- 收集该 section 匹配的图表（按文本中首次出现顺序排列）----
        section_charts = []
        for code6 in codes_in_section:
            if code6 in code_to_chart and code6 not in placed_codes:
                pos = sec_text.find(code6)
                section_charts.append((pos, code_to_chart[code6]))
                placed_codes.add(code6)
        # 按在文本中首次出现位置排序
        section_charts.sort(key=lambda x: x[0])
        section_charts = [c for _, c in section_charts]

        if not section_charts:
            processed_sections.append(sec_text)
            continue

        # ---- 在表格后插入图表 ----
        if table_last_line >= 0:
            # 在表格最后一行之后插入
            insert_pos = table_last_line + 1
        else:
            # 没有表格，追加到 section 末尾
            insert_pos = len(lines)

        chart_md_lines = []
        for name, code, url in section_charts:
            chart_md_lines.append(f'![{name} {code}]({url})')

        # 在插入点后加一个空行分隔
        result_lines = lines[:insert_pos]
        if result_lines and result_lines[-1].strip() != '':
            result_lines.append('')
        result_lines.extend(chart_md_lines)
        result_lines.append('')
        result_lines.extend(lines[insert_pos:])

        processed_sections.append('\n'.join(result_lines))

    result = ''.join(processed_sections)

    # ---- 追加未匹配的图表 ----
    unmatched = []
    for code6, (name, code, url) in code_to_chart.items():
        if code6 not in placed_codes:
            unmatched.append(f'![{name} {code}]({url})')

    if unmatched:
        result = result.rstrip() + '\n\n' + '\n'.join(unmatched) + '\n'
        print(f'[Charts] {len(unmatched)} 张图表未匹配到对应section，已追加到末尾')

    return result


# ============================================================
#  Markdown → HTML（Bloomberg Terminal 风格）
# ============================================================

def markdown_to_html(md):
    """两阶段 Markdown→HTML 转换，支持表格和图片。"""

    # ---- Phase 1: 表格提取 ----
    tables = []
    lines = md.split("\n")
    processed_lines = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if re.match(r"^\|.+\|$", stripped):
            if i + 1 < len(lines) and re.match(r"^\|(?:[\s\-:]+\|)+$", lines[i + 1].strip()):
                header_cells = [c.strip() for c in stripped.split("|")[1:-1]]
                sep_cells = [c.strip() for c in lines[i + 1].strip().split("|")[1:-1]]

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

                html = '<div class="tbl-wrap"><table class="tbl"><thead><tr>'
                for j, cell in enumerate(header_cells):
                    html += f'<th style="text-align:{aligns[j]}">{cell}</th>'
                html += "</tr></thead><tbody>"

                i += 2
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
                continue
        processed_lines.append(raw)
        i += 1

    # ---- Phase 2: 标准解析 ----
    md_clean = "\n".join(processed_lines)
    sections = []
    current_section = {"title": "", "content": []}

    for line in md_clean.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 表格占位符
        tbl_match = re.match(r"^%%TABLE_(\d+)%%$", line)
        if tbl_match:
            idx = int(tbl_match.group(1))
            if idx < len(tables):
                current_section["content"].append(("raw_html", tables[idx]))
            continue

        # 标题
        h_match = re.match(r"^#{1,3}\s+(.+)$", line)
        if h_match:
            if current_section["title"] or current_section["content"]:
                sections.append(current_section)
            current_section = {"title": h_match.group(1), "content": []}
            continue

        # 图片
        img_match = re.match(r"^!\[(.+)\]\((.+)\)$", line)
        if img_match:
            alt = img_match.group(1)
            url = img_match.group(2)
            alt = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", alt)
            current_section["content"].append(("img", alt, url))
            continue

        # Bold
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)

        # 有序列表
        ol_match = re.match(r"^(\d+)\.\s+(.+)$", line)
        if ol_match:
            current_section["content"].append(("ol", ol_match.group(1), ol_match.group(2)))
            continue

        # 无序列表
        ul_match = re.match(r"^[-*]\s+(.+)$", line)
        if ul_match:
            current_section["content"].append(("ul", ul_match.group(1)))
            continue

        if line == "---":
            continue

        current_section["content"].append(("p", line))

    if current_section["title"] or current_section["content"]:
        sections.append(current_section)

    # 渲染
    html_parts = []
    for sec in sections:
        html_parts.append('<div class="sec">')
        if sec["title"]:
            clean_title = re.sub(r"^[一二三四五六七八九十]+[、．.]?\s*", "", sec["title"])
            # 移除 emoji 前缀做样式
            html_parts.append(f'<h3 class="sec-h">{clean_title}</h3>')
        for item in sec["content"]:
            typ = item[0]
            if typ == "ol":
                html_parts.append(
                    f'<div class="ni"><span class="ni-num">{item[1]}</span>'
                    f'<div class="ni-text">{item[2]}</div></div>'
                )
            elif typ == "ul":
                html_parts.append(f'<div class="bi">{item[1]}</div>')
            elif typ == "raw_html":
                html_parts.append(item[1])
            elif typ == "img":
                alt, url = item[1], item[2]
                html_parts.append(
                    f'<div class="chart-img">'
                    f'<img src="{url}" alt="{alt}" loading="lazy" '
                    f'onerror="this.parentElement.style.display=\'none\'">'
                    f'</div>'
                )
            else:
                html_parts.append(f'<p class="para">{item[1]}</p>')
        html_parts.append("</div>")

    return "\n".join(html_parts)


# ============================================================
#  HTML 报告生成 —  Bloomberg Terminal Dark 审美
# ============================================================

def generate_html_report(report, quotes, news_list, page_url="", page_base_url="",
                         northbound=None, fund_flow=None):
    """生成暗色 Bloomberg Terminal 风格 HTML 详情页。"""
    today = datetime.now().strftime("%Y-%m-%d")
    today_en = datetime.now().strftime("%B %d, %Y")
    now_str = datetime.now().strftime("%H:%M")
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[datetime.now().weekday()]
    wk_cn = weekday_cn[datetime.now().weekday()]

    # ---- 历史简报导航 ----
    history_links_html = ""
    if page_base_url:
        for i in range(1, 6):
            d = datetime.now() - timedelta(days=i)
            label = d.strftime("%m月%d日")
            history_links_html += (
                f'<a class="hl" href="{page_base_url}report_{d.strftime("%Y%m%d")}.html">'
                f'{label}</a>'
            )

    # ---- 行情条 ----
    quote_cells = ""
    for q in quotes:
        change_str = q["change"]
        is_up = change_str.startswith("+")
        is_down = change_str.startswith("-") and change_str != "--"
        direction = "up" if is_up else ("dn" if is_down else "")
        arrow = "▲" if is_up else ("▼" if is_down else "─")
        quote_cells += (
            f'<div class="tkr {direction}">'
            f'<span class="tkr-n">{q["name"]}</span>'
            f'<span class="tkr-p">{q["price"]}</span>'
            f'<span class="tkr-c">{arrow} {change_str}</span>'
            f'</div>'
        )

    # ---- 资金面面板 ----
    fund_panel = ""
    if fund_flow:
        fund_panel += '<div class="fp"><div class="fp-h">主力资金流向（近5日 · 亿元）</div><div class="fp-bars">'
        max_val = max(abs(r["net_flow"]) for r in fund_flow) if fund_flow else 1
        for row in fund_flow:
            net = row["net_flow"]
            is_pos = net >= 0
            pct = min(abs(net) / max_val * 100, 100) if max_val else 0
            color = "#00c853" if is_pos else "#ff1744"
            fund_panel += (
                f'<div class="fp-bar-row">'
                f'<span class="fp-date">{row["date"][-5:]}</span>'
                f'<span class="fp-bar-bg"><span class="fp-bar-fill" style="width:{pct}%;background:{color}"></span></span>'
                f'<span class="fp-val" style="color:{color}">{net:+.1f}</span>'
                f'</div>'
            )
        fund_panel += '</div></div>'

    if northbound:
        fund_panel += '<div class="fp"><div class="fp-h">北向资金（近5日 · 亿元）</div><div class="fp-bars">'
        max_val = max(abs(r.get("net_buy_total", 0)) for r in northbound) if northbound else 1
        if max_val > 0:
            for row in northbound:
                net = row.get("net_buy_total", 0)
                is_pos = net >= 0
                pct = min(abs(net) / max_val * 100, 100) if max_val else 0
                color = "#00c853" if is_pos else "#ff1744"
                fund_panel += (
                    f'<div class="fp-bar-row">'
                    f'<span class="fp-date">{row["date"][-5:]}</span>'
                    f'<span class="fp-bar-bg"><span class="fp-bar-fill" style="width:{pct}%;background:{color}"></span></span>'
                    f'<span class="fp-val" style="color:{color}">{net:+.1f}</span>'
                    f'</div>'
                )
        else:
            fund_panel += '<div class="fp-empty">今日非交易日，暂无北向资金数据</div>'
        fund_panel += '</div></div>'

    # ---- 市场统计 ----
    valid_news = [n for n in news_list if "error" not in n]
    a_count = sum(1 for n in valid_news if n.get("market") == "A股")
    us_count = sum(1 for n in valid_news if n.get("market") == "美股")
    hk_count = sum(1 for n in valid_news if n.get("market") == "港股")

    # ---- 分时图 ----
    chart_images = [
        ("上证指数", "https://image.sinajs.cn/newchart/min/n/sh000001.gif"),
        ("深证成指", "https://image.sinajs.cn/newchart/min/n/sz399001.gif"),
    ]
    chart_html = ""
    for name, img_url in chart_images:
        chart_html += (
            f'<div class="ch-cell">'
            f'<div class="ch-label">{name}</div>'
            f'<img src="{img_url}" alt="{name}" loading="lazy" '
            f'onerror="this.parentElement.style.display=\'none\'">'
            f'</div>'
        )

    # ---- 报告正文 ----
    report_html = markdown_to_html(report)

    # ---- 组装 ----
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MARKET BRIEF · {today}</title>
<style>
/* ============================================================
   Bloomberg Terminal Dark — Professional Market Intelligence
   ============================================================ */
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@300;400;500;600;700;800;900&family=Noto+Sans+SC:wght@300;400;500;700;900&display=swap');

:root {{
  --bg: #0d1117;
  --bg-card: #161b22;
  --bg-elevated: #1c2333;
  --border: #21262d;
  --border-light: #30363d;
  --text-primary: #e6edf3;
  --text-secondary: #8b949e;
  --text-muted: #484f58;
  --accent: #ff6b35;
  --accent-blue: #58a6ff;
  --green: #3fb950;
  --red: #f85149;
  --amber: #d2991d;
  --purple: #a371f7;
}}

* {{ margin:0; padding:0; box-sizing:border-box; }}

body {{
  font-family: 'Inter', 'Noto Sans SC', -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text-primary);
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}}

/* ===== TOP BAR ===== */
.topbar {{
  background: #000;
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
}}
.topbar-inner {{
  max-width: 960px; margin:0 auto; padding: 10px 24px;
  display:flex; align-items:center; justify-content:space-between;
}}
.logo {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 15px; font-weight: 700;
  letter-spacing: 2px; color: var(--text-primary);
}}
.logo em {{ color: var(--accent); font-style: normal; }}
.topbar-meta {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px; color: var(--text-muted);
  text-align: right; line-height: 1.5;
}}

/* ===== TICKER STRIP ===== */
.ticker-strip {{
  background: #000; border-bottom: 1px solid var(--border);
  overflow-x: auto; white-space: nowrap;
}}
.ticker-strip::-webkit-scrollbar {{ height:0; }}
.ticker-inner {{
  max-width: 960px; margin:0 auto; padding: 8px 24px;
  display:flex; gap:0;
}}
.tkr {{
  flex:0 0 auto; padding: 6px 18px; text-align:center;
  border-right: 1px solid var(--border); min-width: 120px;
}}
.tkr:last-child {{ border-right:none; }}
.tkr-n {{
  display:block; font-size:10px; color: var(--text-muted);
  letter-spacing: 1px; font-weight: 600;
  text-transform: uppercase; margin-bottom: 3px;
}}
.tkr-p {{
  display:block; font-family: 'JetBrains Mono', monospace;
  font-size: 16px; font-weight: 700; color: var(--text-primary);
}}
.tkr-c {{
  display:block; font-family: 'JetBrains Mono', monospace;
  font-size: 11px; font-weight: 600; margin-top: 2px;
}}
.tkr.up .tkr-c {{ color: var(--red); }}
.tkr.dn .tkr-c {{ color: var(--green); }}

/* ===== HISTORY NAV ===== */
.hnav {{
  max-width:960px; margin:0 auto; padding: 10px 24px;
  border-bottom: 1px solid var(--border);
  display:flex; align-items:center; gap:6px; flex-wrap:wrap;
  background: var(--bg-card);
}}
.hnav-label {{
  font-size:11px; font-weight:700; color: var(--text-muted);
  letter-spacing:1.5px; text-transform:uppercase; margin-right:6px;
}}
.hl {{
  font-family: 'JetBrains Mono', monospace;
  font-size:11px; font-weight:600; color: var(--text-secondary);
  padding:4px 10px; border-radius:2px;
  background: var(--bg); border:1px solid var(--border);
  text-decoration:none; transition: all 0.15s;
}}
.hl:hover {{ color: var(--accent); border-color: var(--accent); }}

/* ===== MASTHEAD ===== */
.masthead {{
  max-width:960px; margin:0 auto; padding: 40px 24px 28px;
  text-align:center; border-bottom: 2px solid var(--border);
}}
.masthead-date {{
  font-family: 'JetBrains Mono', monospace;
  font-size:12px; color: var(--text-muted);
  letter-spacing:3px; text-transform:uppercase;
  font-weight:500; margin-bottom:12px;
}}
.masthead h1 {{
  font-family: 'Inter', 'Noto Sans SC', sans-serif;
  font-size:36px; font-weight:900; color: var(--text-primary);
  letter-spacing: 1px; margin-bottom: 8px;
}}
.masthead-sub {{
  font-size:14px; color: var(--text-secondary);
  font-weight:400; font-family: 'JetBrains Mono', monospace;
  letter-spacing: 1px;
}}
.masthead-tags {{
  margin-top:18px; display:flex; justify-content:center; gap:8px; flex-wrap:wrap;
}}
.mtag {{
  font-family: 'JetBrains Mono', monospace;
  font-size:10px; font-weight:600; letter-spacing:1.5px;
  padding:4px 12px; border-radius:2px; text-transform:uppercase;
}}
.mtag-a {{ background: rgba(255,107,53,0.12); color: var(--accent); border: 1px solid rgba(255,107,53,0.25); }}
.mtag-us {{ background: rgba(88,166,255,0.10); color: var(--accent-blue); border: 1px solid rgba(88,166,255,0.20); }}
.mtag-hk {{ background: rgba(210,153,29,0.10); color: var(--amber); border: 1px solid rgba(210,153,29,0.20); }}

/* ===== CONTENT ===== */
.content {{ max-width:960px; margin:0 auto; padding: 0 24px 40px; }}

/* ===== SECTION HEADER ===== */
.sec-hdr {{
  font-family: 'JetBrains Mono', monospace;
  font-size:10px; font-weight:700; letter-spacing:4px;
  text-transform:uppercase; color: var(--accent);
  padding:24px 0 12px; margin-top:28px;
  border-top: 2px solid var(--border-light);
}}

/* ===== FUND PANEL ===== */
.fp-grid {{
  display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap:16px; margin-top:16px;
}}
.fp {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  padding: 16px 20px;
}}
.fp-h {{
  font-family: 'JetBrains Mono', monospace;
  font-size:11px; font-weight:600; color: var(--text-secondary);
  letter-spacing:1px; margin-bottom:14px;
  text-transform:uppercase;
}}
.fp-bars {{ display:flex; flex-direction:column; gap:6px; }}
.fp-bar-row {{
  display:flex; align-items:center; gap:10px;
}}
.fp-date {{
  font-family: 'JetBrains Mono', monospace;
  font-size:10px; color: var(--text-muted); width:40px; flex-shrink:0;
}}
.fp-bar-bg {{
  flex:1; height:6px; background: var(--bg);
  border-radius: 3px; overflow:hidden;
}}
.fp-bar-fill {{
  display:block; height:100%; border-radius:3px;
  transition: width 0.6s ease;
}}
.fp-val {{
  font-family: 'JetBrains Mono', monospace;
  font-size:12px; font-weight:700; width:55px; text-align:right; flex-shrink:0;
}}
.fp-empty {{
  font-size:12px; color: var(--text-muted);
  padding: 8px 0;
}}

/* ===== CHARTS ROW ===== */
.charts-row {{
  display:grid; grid-template-columns: repeat(2, 1fr); gap:12px;
  margin-top:14px;
}}
.ch-cell {{
  background: var(--bg-card); border: 1px solid var(--border);
}}
.ch-label {{
  font-family: 'JetBrains Mono', monospace;
  font-size:10px; font-weight:700; letter-spacing:1px;
  color: var(--text-secondary); text-align:center;
  padding: 8px 0 4px; border-bottom: 1px solid var(--border);
  text-transform:uppercase;
}}
.ch-cell img {{ width:100%; display:block; filter: brightness(0.9); }}

/* ===== REPORT BODY ===== */
.report-body {{ margin-top:18px; }}

.sec {{ margin-bottom:24px; }}
.sec:last-child {{ margin-bottom:0; }}

.sec-h {{
  font-family: 'Inter', 'Noto Sans SC', sans-serif;
  font-size:18px; font-weight:700; color: var(--text-primary);
  padding-bottom: 8px; margin-bottom: 14px;
  border-bottom: 1px solid var(--border-light);
}}

/* ---- News items ---- */
.ni {{
  display:flex; align-items:flex-start; gap:12px;
  padding:12px 16px; margin:8px 0;
  background: var(--bg-card); border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  transition: border-color 0.2s;
}}
.ni:hover {{ border-left-color: var(--accent-blue); }}
.ni-num {{
  flex-shrink:0; width:22px; height:22px; line-height:22px;
  text-align:center; font-family: 'JetBrains Mono', monospace;
  font-size:12px; font-weight:700; color: var(--accent);
  border: 1px solid var(--border-light); border-radius:2px;
}}
.ni-text {{ font-size:14px; line-height:1.7; color: var(--text-primary); }}
.ni-text strong {{ color: var(--accent); font-weight:700; }}

/* ---- Bullets ---- */
.bi {{
  padding:5px 18px 5px 28px; margin:3px 0; position:relative;
  font-size:14px; line-height:1.7; color: var(--text-secondary);
}}
.bi::before {{
  content:''; position:absolute; left:16px; top:13px;
  width:4px; height:4px; background: var(--accent); opacity:0.7;
}}
.bi strong {{ color: var(--accent-blue); }}

/* ---- Paragraphs ---- */
.para {{
  font-size:14.5px; line-height:1.8; color: var(--text-secondary);
  margin:8px 0;
}}
.para strong {{ color: var(--text-primary); }}

/* ---- Tables ---- */
.tbl-wrap {{
  overflow-x:auto; margin:14px 0;
  border: 1px solid var(--border);
}}
.tbl {{
  width:100%; border-collapse:collapse;
  font-size:13px; line-height:1.6;
  background: var(--bg-card);
}}
.tbl thead {{
  background: var(--bg-elevated);
}}
.tbl th {{
  padding: 10px 12px; font-family: 'JetBrains Mono', 'Inter', sans-serif;
  font-size:11px; font-weight:700; letter-spacing:0.5px;
  color: var(--text-secondary); text-transform:uppercase;
  border-bottom: 1px solid var(--border-light);
  white-space:nowrap;
}}
.tbl td {{
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  color: var(--text-primary);
}}
.tbl tbody tr:hover {{
  background: rgba(255,107,53,0.04);
}}
.tbl td strong {{ color: var(--accent); font-weight:700; }}

/* ---- Chart images ---- */
.chart-img {{
  margin: 14px 0; background: var(--bg-card);
  border: 1px solid var(--border); display:inline-block; max-width:100%;
}}
.chart-img img {{
  max-width:600px; width:100%; height:auto; display:block;
}}

/* ===== FOOTER ===== */
.site-footer {{
  max-width:960px; margin:0 auto; padding: 28px 24px 48px;
  border-top: 2px solid var(--border); text-align:center;
}}
.footer-logo {{
  font-family: 'JetBrains Mono', monospace;
  font-size:13px; font-weight:700; color: var(--text-primary);
  letter-spacing:2px; margin-bottom:10px;
}}
.footer-info {{
  font-size:11px; color: var(--text-muted); line-height:2;
  font-family: 'JetBrains Mono', monospace;
}}
.footer-disclaimer {{
  font-size:10px; color: var(--text-muted); margin-top:14px;
  padding-top:14px; border-top: 1px solid var(--border);
  line-height:1.8;
}}
.online-link {{
  display:inline-block; margin-top:14px;
  padding: 10px 24px; background: var(--accent); color: #fff;
  font-family: 'JetBrains Mono', monospace;
  font-size:12px; font-weight:600; letter-spacing:1.5px;
  text-decoration:none; border-radius:2px;
  text-transform:uppercase;
  transition: background 0.2s;
}}
.online-link:hover {{ background: #e85d2c; }}

/* ===== RESPONSIVE ===== */
@media (max-width: 680px) {{
  .masthead h1 {{ font-size:26px; }}
  .topbar-inner {{ flex-direction:column; gap:4px; text-align:center; }}
  .topbar-meta {{ text-align:center; }}
  .ticker-inner {{ padding:6px 12px; }}
  .tkr {{ min-width:100px; padding:6px 12px; }}
  .tkr-p {{ font-size:14px; }}
  .charts-row {{ grid-template-columns:1fr; }}
  .content {{ padding:0 14px 30px; }}
  .sec-h {{ font-size:16px; }}
  .ni {{ padding:10px 12px; }}
  .fp-grid {{ grid-template-columns:1fr; }}
  .chart-img img {{ max-width:100%; }}
}}
</style>
</head>
<body>

<!-- TOP BAR -->
<div class="topbar">
  <div class="topbar-inner">
    <div class="logo">MARKET<em>//</em>BRIEF</div>
    <div class="topbar-meta">{weekday}<br>{now_str} CST</div>
  </div>
</div>

<!-- TICKER STRIP -->
<div class="ticker-strip">
  <div class="ticker-inner">{quote_cells}</div>
</div>

<!-- HISTORY NAV -->
<div class="hnav">
  <span class="hnav-label">HISTORY</span>
  {history_links_html}
</div>

<!-- MASTHEAD -->
<div class="masthead">
  <div class="masthead-date">{today} · {wk_cn}</div>
  <h1>每日市场情报</h1>
  <div class="masthead-sub">INSTITUTIONAL · MARKET · INTELLIGENCE</div>
  <div class="masthead-tags">
    <span class="mtag mtag-a">A-SHARE · {a_count}</span>
    <span class="mtag mtag-us">US · {us_count}</span>
    <span class="mtag mtag-hk">HK · {hk_count}</span>
  </div>
</div>

<div class="content">

  <!-- FUND FLOW PANEL -->
  <div class="sec-hdr">CAPITAL FLOWS</div>
  <div class="fp-grid">{fund_panel}</div>

  <!-- INTRADAY CHARTS -->
  <div class="sec-hdr">INTRADAY</div>
  <div class="charts-row">{chart_html}</div>

  <!-- AI ANALYSIS -->
  <div class="sec-hdr">ANALYSIS</div>
  <div class="report-body">{report_html}</div>

</div>

<div class="site-footer">
  <div class="footer-logo">MARKET//BRIEF</div>
  {f'<a class="online-link" href="{page_url}">VIEW ONLINE</a>' if page_url else ''}
  <div class="footer-info">
    DATA · East Money / Sina Finance<br>
    AI · DeepSeek V4
  </div>
  <div class="footer-disclaimer">
    本报告由 AI 自动生成，仅供研究参考，不构成任何投资建议。<br>
    市场有风险，投资需谨慎。PAST PERFORMANCE IS NOT INDICATIVE OF FUTURE RESULTS.
  </div>
</div>

</body>
</html>"""
    return html


# ============================================================
#  PDF 生成
# ============================================================

def generate_pdf(html_path):
    """将 HTML 报告转为 PDF（Chrome Headless）。"""
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


# ============================================================
#  GitHub Pages 部署
# ============================================================

def deploy_github_pages(html_content):
    """将 HTML 写入 docs/ 目录。"""
    today = datetime.now().strftime("%Y%m%d")
    os.makedirs("docs", exist_ok=True)

    report_path = f"docs/report_{today}.html"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    # index.html → 最新报告
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html_content)

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


# ============================================================
#  推送通知
# ============================================================

def send_wechat(report, quotes, news_list, page_url):
    """通过 Server酱 推送微信消息。"""
    send_key = os.environ.get("SERVERCHAN_KEY", "")
    if not send_key:
        print("未设置 SERVERCHAN_KEY，跳过微信推送")
        return

    today = datetime.now().strftime("%Y年%m月%d日")
    title = f"📈 每日市场情报 | {today}"

    # 行情表
    lines = ["## 📊 行情一览\n"]
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

    # 分时图
    lines.append("![上证指数](https://image.sinajs.cn/newchart/min/n/sh000001.gif)")
    lines.append("")

    # 报告摘要
    lines.append("---\n")
    lines.append("## 🤖 AI 分析\n")
    lines.append(report[:3000])  # 微信限制
    lines.append("")

    # 链接
    lines.append("---\n")
    if page_url:
        lines.append(f"### 🔗 [查看完整报告]({page_url})\n")
    lines.append(f"> 📅 {today} · {len(news_list)}条资讯 · AI分析仅供参考")

    desp = "\n".join(lines)

    url = f"https://sctapi.ftqq.com/{send_key}.send"
    payload = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
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


def send_webhook(text):
    """推送 Webhook（钉钉/飞书/企业微信）。"""
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    if not webhook_url:
        return
    if "qyapi.weixin" in webhook_url:
        payload = json.dumps({"msgtype": "markdown", "markdown": {"content": text[:4096]}})
    elif "dingtalk" in webhook_url:
        payload = json.dumps({"msgtype": "markdown", "markdown": {"title": "市场情报", "text": text[:4096]}})
    elif "feishu" in webhook_url or "larksuite" in webhook_url:
        payload = json.dumps({"msg_type": "text", "content": {"text": text[:4096]}})
    else:
        payload = json.dumps({"text": text[:4096]})

    req = urllib.request.Request(
        webhook_url, data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Webhook 推送成功: {resp.status}")
    except Exception as e:
        print(f"Webhook 推送失败: {e}")


# ============================================================
#  主流程
# ============================================================

def main():
    print(f"[{datetime.now()}] 开始生成每日市场情报...")
    print()

    # 1. 行情
    print("▸ 抓取指数行情...")
    quotes = fetch_index_quotes()
    for q in quotes:
        print(f"  {q['name']}: {q['price']} ({q['change']})")

    # 2. 新闻 + 资金面
    print("\n▸ 抓取多市场新闻 & 资金面数据...")
    news_list, northbound, fund_flow = fetch_all_news()

    a_news = [n for n in news_list if n.get("market") == "A股" and "error" not in n]
    us_news = [n for n in news_list if n.get("market") == "美股" and "error" not in n]
    hk_news = [n for n in news_list if n.get("market") == "港股" and "error" not in n]
    print(f"  A股: {len(a_news)}条 | 美股: {len(us_news)}条 | 港股: {len(hk_news)}条")
    if fund_flow:
        latest = fund_flow[-1]
        direction = "流入" if latest["net_flow"] >= 0 else "流出"
        print(f"  主力资金({latest['date']}): {direction} {abs(latest['net_flow']):.1f}亿")
    if northbound:
        latest_nb = northbound[-1]
        direction = "流入" if latest_nb.get("net_buy_total", 0) >= 0 else "流出"
        nb_total = abs(latest_nb.get("net_buy_total", 0))
        if nb_total > 0:
            print(f"  北向资金({latest_nb['date']}): {direction} {nb_total:.1f}亿")

    if not a_news and not us_news and not hk_news:
        print("没有抓取到任何新闻，退出")
        return

    # 3. 格式化 & LLM 分析
    news_text = format_news(news_list, northbound, fund_flow)
    print("\n▸ 生成 AI 市场分析...")
    report = call_llm(news_text)

    # 4. AI 选股
    print("▸ 执行 AI 产业链选股...")
    stock_picks = call_stock_picker(news_text)

    # 预先构造 GitHub Pages URL
    today_str = datetime.now().strftime("%Y%m%d")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        owner = repo.split("/")[0].lower()
        repo_name = repo.split("/")[1]
        page_base_url = f"https://{owner}.github.io/{repo_name}/"
    else:
        page_base_url = "https://hcongxi42-web.github.io/HZT/"

    # 5. 技术分析图表
    if stock_picks and stock_analyzer:
        print("  解析高评分股票...")
        stock_list = stock_analyzer.parse_stock_picks(stock_picks)
        if stock_list:
            print(f"  发现 {len(stock_list)} 只高评分股票，生成技术分析图...")
            charts_dir = os.path.join("docs", "charts")
            chart_urls = stock_analyzer.analyze_stocks(stock_list, charts_dir, page_base_url)
            if chart_urls:
                stock_picks = insert_charts_into_picks(stock_picks, chart_urls)
                print(f"  已生成 {len(chart_urls)} 张 K 线图")
            else:
                print("  未能生成任何图表")
        else:
            print("  未解析到高评分股票")
    elif not stock_analyzer:
        print("  stock_analyzer 模块未加载，跳过图表")

    # 组装完整报告
    if stock_picks:
        report += format_stock_picks(stock_picks)
        print("  AI 选股完成")

    print("\n" + "=" * 60)
    print(report[:2000])
    if len(report) > 2000:
        print(f"... (总 {len(report)} 字符)")
    print("=" * 60)

    # 6. HTML
    print("\n▸ 生成详情页...")
    page_url = f"{page_base_url}report_{today_str}.html"
    html = generate_html_report(report, quotes, news_list, page_url, page_base_url,
                                northbound, fund_flow)
    page_url = deploy_github_pages(html)

    # 7. PDF
    print("▸ 生成 PDF...")
    html_file = f"docs/report_{today_str}.html"
    generate_pdf(html_file)

    # 8. 保存 Markdown
    report_file = f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(f"# 每日市场情报 - {datetime.now().strftime('%Y-%m-%d')}\n\n")
        f.write(report)
    print(f"Markdown 报告: {report_file}")

    # 9. GitHub Actions output
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"report_file={report_file}\n")
            f.write(f"page_url={page_url}\n")

    # 10. 推送
    send_wechat(report, quotes, news_list, page_url)
    send_webhook(report)

    print(f"\n[{datetime.now()}] 完成")


if __name__ == "__main__":
    main()
