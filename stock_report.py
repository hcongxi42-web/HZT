"""
独立版股市日报生成器 - 用于 GitHub Actions 定时运行
多市场新闻聚合 + DeepSeek AI 分析 + 技术图表 + GitHub Pages 部署
支持微信推送（Server酱）+ 多平台 Webhook
"""

import json
import os
import re
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
#  工具函数
# ============================================================

def get_session_label():
    """根据北京时间判断报告场次（A股交易时段）。"""
    try:
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
    except Exception:
        hour = (datetime.utcnow().hour + 8) % 24
    if hour < 12:
        return "早报", "am"
    else:
        return "晚报", "pm"


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
    articles, errors, fund_flow = fetch_all_news_flat("all")
    for err in errors:
        print(f"  [WARN] {err.get('error', str(err))}")
    return articles, fund_flow


def _fmt_time_short(time_str):
    """将各种时间格式统一为 MM-DD HH:MM 短格式。"""
    if not time_str:
        return ""
    import re as _re
    # 2026-06-09 10:55:00 → 06-09 10:55
    m = _re.match(r'\d{4}-(\d{2}-\d{2})\s+(\d{2}:\d{2})', time_str)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    # 06-09 10:55 → 原样
    m = _re.match(r'\d{2}-\d{2}\s+\d{2}:\d{2}', time_str)
    if m:
        return time_str[:11]
    # 2026/06/09 10:55 → 06-09 10:55
    m = _re.match(r'\d{4}/(\d{2}/\d{2})\s+(\d{2}:\d{2})', time_str)
    if m:
        return f"{m.group(1).replace('/', '-')} {m.group(2)}"
    return time_str[:11] if len(time_str) >= 11 else time_str


def format_news(news_list, fund_flow=None):
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

        for i, a in enumerate(articles[:30], 1):
            title = a.get("title", "")
            summary = a.get("summary", "")
            time_str = a.get("time", "")
            src = a.get("source", "")
            short_time = _fmt_time_short(time_str)

            # 格式：[来源] MM-DD HH:MM : 标题
            if short_time:
                line = f"{i}. [{src}] {short_time} : {title}"
            else:
                line = f"{i}. [{src}] {title}"
            lines.append(line)
            # 摘要（如果与标题不同且有内容）
            if summary and summary != title and len(summary) > 10:
                lines.append(f"   {summary[:200]}")

    return "\n".join(lines)


# ============================================================
#  LLM 分析 — 提示词
# ============================================================

SYSTEM_PROMPT = (
    "你是一位资深买方策略师，有 20 年 A 股投研经验。"
    "你写报告的风格是：一针见血、不堆术语、用常识说话。"
    "你不写废话，不套模板，每句话都要有信息量。"
)

USER_PROMPT_TEMPLATE = """基于以下资讯，写一份简短的盘中情报，直接给判断，别啰嗦。

结构：
- 资金面：1-2 句判断主力态度（进攻/防守/观望），提到关键数据支撑
- 主线扫描：今天最值得关注的 3-5 条方向，点出方向的逻辑和持续性
- 跨市场：美股/港股如有异动，说一下对 A 股可能的传导
- 要闻速览：挑最重要的 8-10 条新闻，每条一行，标利好/利空/中性
- 市场体温：给一个整体判断（偏热/偏暖/中性/偏冷/冰点），给出合理的逻辑推导和数据支持
- 明天怎么看：2-3 种可能的情景推演

风格：
- 别用「值得关注的是」「总体来看」「综合来看」这种废话开头
- 别用 emoji
- 用「」标记股票和关键术语
- 像给人发微信一样说话，不像在写论文

---
数据：

{news_text}"""


# ============================================================
#  AI 选股 — 提示词
# ============================================================

STOCK_PICKER_SYSTEM_PROMPT = (
    "你是一位专攻产业链研究的买方分析师，"
    "擅长从一条新闻出发，顺藤摸瓜找出整个供应链上真正受益或受损的公司。"
    "你推的每只股票都有据可查，不拍脑袋，不编代码。"
)

STOCK_PICKER_TEMPLATE = """从以下资讯中，找出今天值得关注的个股机会和风险。

怎么找：
- 先看新闻里直接提到了哪些公司，如果有直接提及，分析这个新闻对公司的影响
- 再沿产业链上下推导：供应商、客户、竞争对手、替代品玩家
- 结合资金流向，优先挑资金在买的板块
- 禁止编造新闻里不存在的股票

怎么标记优先级（别用 emoji，用文字）：
- 「超核心关注」：公司位于产业链中的卡脖子环节，缺少了这个公司或其产品，会剧烈影响整个产业链的运作
- 「核心关注」：逻辑直接、短期可能反应到产业链
- 「可以看看」：产业链传导受益，逻辑成立但稍远
- 「知道就行」：沾边但逻辑链条长

输出格式：
- 每条重要新闻用 ### 做标题
- 新闻下面放表格：| 股票及代码 | 为什么选它 | 看多/看空 | 把握度 |
- 「超核心关注」、「核心关注」和「可以看看」放表格里
- 「知道就行」的放在新闻末尾用 - 简单提一下
- 如果有利空消息，用 ### 个股风险提示 单独列出（减持/业绩暴雷/监管处罚等）

注意：
- 表格每栏至少 3-5 行（可以更多），体现产业链推理
- 每条资讯只用一次，别在不同板块里重复

---
数据：

{news_text}"""


# ============================================================
#  LLM 输出清洗
# ============================================================

def _cleanup_report(text, strip_bold=False):
    """清洗 LLM 生成的报告：移除 #### / *** 标记。

    Args:
        text: 原始文本
        strip_bold: 是否移除 ** 加粗标记（选股输出用，主报告保留）
    """
    import re as _re

    # 1. 移除 #### 前缀（四级标题 → 保留其后的内容）
    text = _re.sub(r'^####\s+', '', text, flags=_re.MULTILINE)

    # 2. 移除独立的 *** 分隔线（整行只有 ***，允许前后空白）
    text = _re.sub(r'^\s*\*{3}\s*$', '', text, flags=_re.MULTILINE)

    # 3. （可选）移除 ** 标记 — 选股表格中 LLM 习惯给每个字段加粗
    if strip_bold:
        text = _re.sub(r'\*\*', '', text)

    # 4. 清理可能产生的多余空行（连续 3+ 空行 → 2 个空行）
    text = _re.sub(r'\n{3,}', '\n\n', text)

    return text


def _highlight_inline(text):
    """为关键判断词添加彩色高亮标记。

    覆盖：利好/利空/中性、市场体温、主力态度等。
    返回带 <mark class="mk-*"> 的 HTML 片段。
    """
    import re as _re

    # 利好 / 利空 / 中性（颜色标签）
    text = _re.sub(r'(利好)', r'<mark class="mk-bullish">\1</mark>', text)
    text = _re.sub(r'(利空)', r'<mark class="mk-bearish">\1</mark>', text)
    text = _re.sub(r'(?<![a-zA-Z\d])中性(?![a-zA-Z\d])',
                   r'<mark class="mk-neutral">中性</mark>', text)

    # 市场体温
    text = _re.sub(r'(偏热)', r'<mark class="mk-hot">\1</mark>', text)
    text = _re.sub(r'(偏暖)', r'<mark class="mk-warm">\1</mark>', text)
    text = _re.sub(r'(偏冷)', r'<mark class="mk-cool">\1</mark>', text)
    text = _re.sub(r'(?<![a-zA-Z\d])冰点(?![a-zA-Z\d])',
                   r'<mark class="mk-ice">冰点</mark>', text)

    # 主力态度
    text = _re.sub(r'(进攻)', r'<mark class="mk-bullish">\1</mark>', text)
    text = _re.sub(r'(防守)', r'<mark class="mk-bearish">\1</mark>', text)
    text = _re.sub(r'(观望)', r'<mark class="mk-neutral">\1</mark>', text)

    return text


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
    """调用 LLM 生成市场分析报告，并清理 #### / *** 标记。"""
    raw = _call_deepseek(SYSTEM_PROMPT, USER_PROMPT_TEMPLATE.format(news_text=news_text),
                         temperature=0.5, max_tokens=4096)
    return _cleanup_report(raw)


def call_stock_picker(news_text):
    """调用 LLM 执行产业链选股分析，并清洗输出（含去 **）。"""
    raw = _call_deepseek(STOCK_PICKER_SYSTEM_PROMPT, STOCK_PICKER_TEMPLATE.format(news_text=news_text),
                         temperature=0.3, max_tokens=6144)
    return _cleanup_report(raw, strip_bold=True)


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
    first_chart = True  # 在第一张图上方标注保留期限

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
            if first_chart:
                chart_md_lines.append('<span style="color:#d63031;font-weight:600;">⚠️ 注：K线图仅保留近7天，历史图表将自动清理。如需长期保存，请右键另存为。</span>')
                first_chart = False
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
            current_section["content"].append(("ol", ol_match.group(1), _highlight_inline(ol_match.group(2))))
            continue

        # 无序列表
        ul_match = re.match(r"^[-*]\s+(.+)$", line)
        if ul_match:
            current_section["content"].append(("ul", _highlight_inline(ul_match.group(1))))
            continue

        if line == "---":
            continue

        current_section["content"].append(("p", _highlight_inline(line)))

    if current_section["title"] or current_section["content"]:
        sections.append(current_section)

    # 渲染
    import hashlib as _hl
    html_parts = []
    for sec_idx, sec in enumerate(sections):
        sec_title = sec.get("title", "")
        # 生成唯一 section ID（基于标题 hash）
        sec_id = _hl.md5(sec_title.encode()).hexdigest()[:10] if sec_title else f"s{sec_idx}"
        html_parts.append(f'<div class="sec" data-section-id="{sec_id}">')
        if sec["title"]:
            clean_title = re.sub(r"^[一二三四五六七八九十]+[、．.]?\s*", "", sec["title"])
            html_parts.append(
                f'<h3 class="sec-h">'
                f'<button class="fav-btn" data-sid="{sec_id}" '
                f'title="收藏此条分析" onclick="toggleFav(this)">☆</button>'
                f'{clean_title}</h3>'
            )
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
                         fund_flow=None, session_label="早报", session_slug="am"):
    """生成 Bloomberg Terminal 风格 HTML 详情页。"""
    today = datetime.now().strftime("%Y-%m-%d")
    today_en = datetime.now().strftime("%B %d, %Y")
    now_str = datetime.now().strftime("%H:%M")
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[datetime.now().weekday()]
    wk_cn = weekday_cn[datetime.now().weekday()]
    fav_date_key = f"{today}_{session_slug}"  # 用于收藏唯一标识

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
        # 日期选择器 + 早报/晚报切换按钮
        today_str = datetime.now().strftime("%Y-%m-%d")
        am_active = " active" if session_slug == "am" else ""
        pm_active = " active" if session_slug == "pm" else ""
        history_links_html += (
            f'<span class="hnav-spacer"></span>'
            f'<input type="date" id="historyPicker" class="hl-date" '
            f'value="{today_str}" max="{today_str}" min="2025-01-01" '
            f'title="选择日期查看历史简报">'
            f'<button class="hl-go" onclick="goToDate()">GO</button>'
            f'<span class="session-toggle">'
            f'<button class="st-btn{am_active}" id="stAm" onclick="switchSession(\'am\')">早报</button>'
            f'<button class="st-btn{pm_active}" id="stPm" onclick="switchSession(\'pm\')">晚报</button>'
            f'</span>'
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
<title>MARKET BRIEF · {today} {session_label}</title>
<style>
/* ============================================================
   Institutional Research — Clean Light Theme
   ============================================================ */
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@300;400;500;600;700;800;900&family=Noto+Sans+SC:wght@300;400;500;700;900&display=swap');

:root {{
  --bg: #f7f8fa;
  --bg-card: #ffffff;
  --bg-elevated: #eef0f4;
  --border: #dde1e8;
  --border-light: #e4e7ed;
  --text-primary: #1a1d24;
  --text-secondary: #4e5460;
  --text-muted: #9096a2;
  --accent: #e85d2c;
  --accent-blue: #2070cc;
  --green: #1a8a3f;
  --red: #d63031;
  --amber: #b8860b;
  --purple: #7c3aed;
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
  background: #fff;
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
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
  background: #fff; border-bottom: 1px solid var(--border);
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
.hnav-spacer {{
  flex:1; min-width:12px;
}}
.hl-date {{
  font-family: 'JetBrains Mono', monospace;
  font-size:11px; color: var(--text-primary);
  padding:4px 8px; border-radius:2px;
  background: var(--bg); border:1px solid var(--border);
  outline:none; transition: border-color 0.15s;
  color-scheme: light;
}}
.hl-date:focus {{ border-color: var(--accent); }}
.hl-date::-webkit-calendar-picker-indicator {{
  cursor:pointer;
}}
.hl-go {{
  font-family: 'JetBrains Mono', monospace;
  font-size:11px; font-weight:700; color: #fff;
  padding:4px 12px; border-radius:2px;
  background: var(--accent); border:none;
  cursor:pointer; transition: background 0.15s;
  letter-spacing: 1px;
}}
.hl-go:hover {{ background: #e85d2c; }}

/* ---- Session toggle (早报/晚报) ---- */
.session-toggle {{
  display: inline-flex; border: 1px solid var(--border);
  border-radius: 2px; overflow: hidden; margin-left: 2px;
}}
.st-btn {{
  font-family: 'JetBrains Mono', 'Inter', sans-serif;
  font-size: 11px; font-weight: 600;
  padding: 4px 12px; background: var(--bg);
  border: none; color: var(--text-secondary);
  cursor: pointer; transition: all 0.15s;
  border-right: 1px solid var(--border);
}}
.st-btn:last-child {{ border-right: none; }}
.st-btn.active {{
  background: var(--accent); color: #fff;
}}
.st-btn:hover:not(.active) {{
  color: var(--accent); background: var(--bg-elevated);
}}

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
.ch-cell img {{ width:100%; display:block; }}

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
.ni-text strong {{ color: var(--accent); font-weight:700; background: rgba(232,93,44,0.05); padding: 0 2px; }}

/* ---- Bullets ---- */
.bi {{
  padding:5px 18px 5px 28px; margin:3px 0; position:relative;
  font-size:14px; line-height:1.7; color: var(--text-secondary);
}}
.bi::before {{
  content:''; position:absolute; left:16px; top:13px;
  width:4px; height:4px; background: var(--accent); opacity:0.7;
}}
.bi strong {{ color: var(--accent-blue); font-weight: 700; background: rgba(32,112,204,0.05); padding: 0 2px; }}

/* ---- Paragraphs ---- */
.para {{
  font-size:14.5px; line-height:1.8; color: var(--text-secondary);
  margin:8px 0;
}}
.para strong {{ color: var(--accent); font-weight: 700; background: rgba(232,93,44,0.06); padding: 0 2px; border-radius: 1px; }}

/* ---- Color highlight marks ---- */
mark {{ background: transparent; }}
.mk-bullish {{
  color: var(--green); font-weight: 700;
  background: rgba(26,138,63,0.08); padding: 1px 5px; border-radius: 2px;
}}
.mk-bearish {{
  color: var(--red); font-weight: 700;
  background: rgba(214,48,49,0.08); padding: 1px 5px; border-radius: 2px;
}}
.mk-neutral {{
  color: var(--text-muted); font-weight: 600;
  background: rgba(144,150,162,0.08); padding: 1px 5px; border-radius: 2px;
}}
.mk-hot {{
  color: var(--red); font-weight: 700;
  background: rgba(214,48,49,0.06); padding: 0 3px; border-radius: 2px;
}}
.mk-warm {{
  color: var(--amber); font-weight: 700;
  background: rgba(184,134,11,0.06); padding: 0 3px; border-radius: 2px;
}}
.mk-cool {{
  color: var(--accent-blue); font-weight: 600;
  background: rgba(32,112,204,0.06); padding: 0 3px; border-radius: 2px;
}}
.mk-ice {{
  color: var(--purple); font-weight: 700;
  background: rgba(124,58,237,0.06); padding: 0 3px; border-radius: 2px;
}}

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
  .fav-panel {{ display:none; }}
}}

/* ===== FAVORITES PANEL ===== */
.fav-btn {{
  display:inline-block; background:none; border:none;
  font-size:16px; cursor:pointer; padding:0 6px; margin-right:2px;
  color: var(--text-muted); transition: all 0.2s;
  vertical-align: middle; line-height:1;
}}
.fav-btn:hover {{ color: var(--amber); transform: scale(1.2); }}
.fav-btn.on {{ color: var(--amber); }}

.fav-panel {{
  max-width:960px; margin:0 auto; padding: 0 24px 32px;
}}
.fav-panel-inner {{
  background: var(--bg-card); border: 1px solid var(--border);
  border-top: 3px solid var(--amber);
  border-radius: 0 0 4px 4px;
}}
.fav-panel-hdr {{
  display:flex; align-items:center; justify-content:space-between;
  padding: 12px 20px; border-bottom: 1px solid var(--border);
  font-family: 'JetBrains Mono', monospace;
  font-size:11px; font-weight:700; color: var(--text-secondary);
  letter-spacing:1.5px; text-transform:uppercase;
}}
.fav-panel-hdr em {{ color: var(--amber); font-style:normal; }}
.fav-count {{ font-size:11px; color: var(--text-muted); }}
.fav-toggle {{
  background:none; border:none; color: var(--text-muted);
  font-size:18px; cursor:pointer; padding:0 4px; line-height:1;
  transition: color 0.15s;
}}
.fav-toggle:hover {{ color: var(--text-primary); }}
.fav-empty {{
  text-align:center; padding: 24px; color: var(--text-muted);
  font-size:13px;
}}
.fav-list {{ display:flex; flex-direction:column; }}
.fav-item {{
  display:flex; align-items:flex-start; gap:10px;
  padding:10px 20px; border-bottom: 1px solid var(--border);
  transition: background 0.15s;
}}
.fav-item:last-child {{ border-bottom:none; }}
.fav-item:hover {{ background: var(--bg); }}
.fav-item-date {{
  font-family: 'JetBrains Mono', monospace;
  font-size:10px; color: var(--text-muted); flex-shrink:0;
  min-width:42px; padding-top:2px;
}}
.fav-item-title {{
  flex:1; font-size:13px; font-weight:600; color: var(--text-primary);
  cursor:pointer; line-height:1.5;
}}
.fav-item-title:hover {{ color: var(--accent); }}
.fav-item-del {{
  background:none; border:1px solid var(--border); color: var(--text-muted);
  font-size:10px; cursor:pointer; padding:2px 8px; border-radius:2px;
  flex-shrink:0; transition: all 0.15s;
  font-family: 'JetBrains Mono', monospace;
}}
.fav-item-del:hover {{ color: var(--red); border-color: var(--red); }}

/* ---- Fav action buttons ---- */
.fav-act-btn {{
  font-family: 'JetBrains Mono', monospace;
  font-size:9px; font-weight:600; letter-spacing:0.5px;
  padding:2px 8px; border-radius:2px;
  background: var(--bg); border:1px solid var(--border);
  color: var(--text-muted); cursor:pointer;
  transition: all 0.15s; white-space:nowrap;
}}
.fav-act-btn:hover {{ color: var(--accent); border-color: var(--accent); }}
.fav-act-del:hover {{ color: var(--red); border-color: var(--red); }}

/* ===== FAV TOAST ===== */
.fav-toast {{
  position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
  background: var(--bg-elevated); color: var(--text-primary);
  border: 1px solid var(--border); border-radius:4px;
  padding:8px 20px; font-size:12px; font-weight:600;
  z-index:999; opacity:0; transition: opacity 0.3s;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08); pointer-events:none;
}}
.fav-toast.show {{ opacity:1; }}

@media (max-width: 640px) {{
  .fav-item {{ padding:10px 12px; gap:6px; }}
  .fav-item-date {{ min-width:36px; font-size:9px; }}
  .fav-item-title {{ font-size:12px; }}
  .fav-panel-hdr {{ padding:10px 14px; }}
  .fav-panel {{ padding: 0 14px 24px; }}
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
  <div class="masthead-date">{today} · {wk_cn} · {session_label}</div>
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

<!-- ══════════════ 自选新闻面板 ══════════════ -->
<div class="fav-panel" id="favPanel">
  <div class="fav-panel-inner">
    <div class="fav-panel-hdr">
      <span>⭐ 自选新闻 · <em id="favCount">0</em></span>
      <div style="display:flex;align-items:center;gap:8px;">
        <span class="fav-count" id="favSize" title="localStorage 占用"></span>
        <button class="fav-act-btn" onclick="exportFavs()" title="导出收藏为 JSON 文件">↗导出</button>
        <button class="fav-act-btn" onclick="importFavs()" title="从 JSON 文件导入收藏（合并去重）">↘导入</button>
        <button class="fav-act-btn fav-act-del" onclick="clearAllFavs()" title="清空全部收藏">清空</button>
        <button class="fav-toggle" id="favToggle" title="收起面板" onclick="toggleFavPanel()">▾</button>
      </div>
    </div>
    <div id="favActions" style="padding:6px 20px;font-size:10px;color:var(--text-muted);border-bottom:1px solid var(--border);">
      收藏仅保存索引（不存HTML）· 上限 200 条 · 跨报告跳转需联网 · 可导出备份
    </div>
    <div class="fav-list" id="favList">
      <div class="fav-empty">暂无收藏 · 点击报告中任意分析板块旁的 ☆ 即可收藏</div>
    </div>
  </div>
</div>

<!-- ══════════════ 收藏提示浮层 ══════════════ -->
<div class="fav-toast" id="favToast"></div>

<script>
// ═══════════════════════════════════════════════════════════════
//  自选新闻 · localStorage 持久化（轻量版）
//  只存元数据索引，不存 HTML 副本 — 跨报告跳转时从源文件加载。
// ═══════════════════════════════════════════════════════════════
const STORAGE_KEY = 'market_brief_favs_v2';  // v2: 不再存储 html 字段
const MAX_FAVS = 200;
const FAV_DATE_KEY = '{fav_date_key}';
const FAV_DATE = '{today}';
const FAV_SESSION = '{session_label}';

// ── 早报 / 晚报 切换 ──
let CURRENT_SESSION = '{session_slug}';

function switchSession(session) {{
  if (session === CURRENT_SESSION) return;
  CURRENT_SESSION = session;
  document.getElementById('stAm').classList.toggle('active', session === 'am');
  document.getElementById('stPm').classList.toggle('active', session === 'pm');
  const picker = document.getElementById('historyPicker');
  if (picker && picker.value) {{
    const p = picker.value.split('-');
    window.location.href = '{page_base_url}report_' + p[0] + p[1] + p[2] + '_' + session + '.html';
  }}
}}

function goToDate() {{
  const d = document.getElementById('historyPicker').value;
  if (d) {{
    const p = d.split('-');
    window.location.href = '{page_base_url}report_' + p[0] + p[1] + p[2] + '_' + CURRENT_SESSION + '.html';
  }}
}}

// ── 读写 localStorage（带错误处理 + 旧格式迁移）──
function getFavs() {{
  try {{
    let raw = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (!Array.isArray(raw)) return [];
    // 迁移 v1 → v2: 丢弃 html 字段，只保留元数据
    let migrated = false;
    raw = raw.map(f => {{
      if (f.html !== undefined) {{ migrated = true; }}
      return {{
        id: f.id || '',
        sid: f.sid || '',
        date: f.date || '',
        session: f.session || '',
        title: f.title || '',
        saved_at: f.saved_at || ''
      }};
    }});
    if (migrated) {{
      saveFavsRaw(raw);
      console.log('[Favs] 已从 v1 迁移到 v2 (丢弃HTML副本)');
    }}
    return raw;
  }} catch(e) {{ return []; }}
}}

function saveFavsRaw(favs) {{
  try {{
    localStorage.setItem(STORAGE_KEY, JSON.stringify(favs));
    renderFavPanel();
    updateAllStarBtns();
  }} catch(e) {{
    // localStorage 满了 → 裁剪最旧的 25%
    if (e.name === 'QuotaExceededError') {{
      const drop = Math.ceil(favs.length * 0.25);
      const trimmed = favs.slice(drop);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
      showToast('存储已满，已自动清理' + drop + '条旧收藏');
      renderFavPanel();
      updateAllStarBtns();
    }}
  }}
}}

function saveFavs(favs) {{
  saveFavsRaw(favs);
}}

// ── 估算存储占用 ──
function estimateStorage() {{
  try {{
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? new Blob([raw]).size : 0;
  }} catch(e) {{ return 0; }}
}}

function fmtSize(bytes) {{
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/(1024*1024)).toFixed(1) + ' MB';
}}

// ── 从 sec-h 提取纯文本标题 ──
function getTitleFromSec(sid) {{
  const sec = document.querySelector(`[data-section-id="${{sid}}"]`);
  if (!sec) return '';
  const hEl = sec.querySelector('.sec-h');
  if (!hEl) return '';
  const btn = hEl.querySelector('.fav-btn');
  const btnText = btn ? btn.textContent : '';
  return (hEl.textContent || '').replace(btnText, '').trim();
}}

function makeFavId(sid) {{
  return FAV_DATE_KEY + '_' + sid;
}}

// ── 收藏 / 取消收藏 ──
function toggleFav(btn) {{
  const sid = btn.getAttribute('data-sid');
  const favId = makeFavId(sid);
  const title = getTitleFromSec(sid);
  if (!title) return;

  let favs = getFavs();
  const idx = favs.findIndex(f => f.id === favId);
  if (idx >= 0) {{
    favs.splice(idx, 1);
    saveFavs(favs);
    showToast('已取消收藏');
  }} else {{
    if (favs.length >= MAX_FAVS) {{
      showToast('收藏已达上限 (' + MAX_FAVS + '条)，请先清理旧收藏');
      return;
    }}
    favs.push({{
      id: favId,
      sid: sid,
      date: FAV_DATE,
      session: FAV_SESSION,
      title: title,
      saved_at: new Date().toISOString()
    }});
    saveFavs(favs);
    showToast('已加入自选 ⭐');
  }}
}}

// ── 删除收藏项 ──
function delFav(favId) {{
  let favs = getFavs();
  favs = favs.filter(f => f.id !== favId);
  saveFavs(favs);
  showToast('已删除');
}}

// ── 跳转到收藏项所在报告 ──
function gotoFav(favId) {{
  const favs = getFavs();
  const f = favs.find(x => x.id === favId);
  if (!f) return;
  const sid = f.sid || '';
  const dateDigits = f.date.replace(/-/g, '');

  // 当前页面已有该 section → 直接滚动
  const local = document.querySelector(`[data-section-id="${{sid}}"]`);
  if (local) {{
    local.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
    local.style.boxShadow = '0 0 0 3px var(--amber)';
    setTimeout(() => local.style.boxShadow = '', 2000);
    return;
  }}

  // 跨报告跳转：构建目标 URL
  const sessionSuffix = (f.session === '晚报') ? '_pm' : '_am';
  const targetUrl = '{page_base_url}report_' + dateDigits + sessionSuffix + '.html';
  window.location.href = targetUrl + '#' + sid;
}}

// ── 渲染底部自选面板 ──
function renderFavPanel() {{
  const favs = getFavs();
  const countEl = document.getElementById('favCount');
  const listEl = document.getElementById('favList');
  const summaryEl = document.getElementById('favSummary');
  const panel = document.getElementById('favPanel');
  const sizeEl = document.getElementById('favSize');

  const storageSize = estimateStorage();
  if (countEl) countEl.textContent = favs.length + '条';
  if (sizeEl) sizeEl.textContent = fmtSize(storageSize);
  if (summaryEl) summaryEl.textContent = favs.length
    ? favs.map(f => f.date.slice(5) + (f.session === '晚报' ? '晚' : '早')).slice(-10).join(' · ')
    : '';
  if (panel && favs.length > 0) panel.style.display = 'block';
  else if (panel && favs.length === 0) panel.style.display = 'none';

  if (!listEl) return;
  if (favs.length === 0) {{
    listEl.innerHTML = '<div class="fav-empty">暂无收藏 · 点击报告中任意分析板块旁的 ☆ 即可收藏</div>';
    return;
  }}

  // 最新在前，最多展示最近 100 条
  const sorted = [...favs].reverse().slice(0, 100);
  listEl.innerHTML = sorted.map(f => `
    <div class="fav-item">
      <span class="fav-item-date">${{f.date.slice(5)}}${{f.session === '晚报' ? '晚' : '早'}}</span>
      <span class="fav-item-title" onclick="gotoFav('${{f.id}}')" title="点击跳转到 ${{f.date}} ${{f.session}} · ${{f.title}}">${{f.title}}</span>
      <button class="fav-item-del" onclick="delFav('${{f.id}}')" title="删除">✕</button>
    </div>
  `).join('');

  // 如果超过 100 条，显示提示
  if (favs.length > 100) {{
    listEl.innerHTML += '<div class="fav-item" style="color:var(--text-muted);font-size:11px;justify-content:center;">… 还有 ' + (favs.length - 100) + ' 条更早的收藏（已折叠）</div>';
  }}
}}

// ── 批量清空 ──
function clearAllFavs() {{
  if (confirm('确定要清空全部收藏吗？此操作不可恢复。')) {{
    localStorage.removeItem(STORAGE_KEY);
    renderFavPanel();
    updateAllStarBtns();
    showToast('已清空全部收藏');
  }}
}}

// ── 导出收藏为 JSON 文件 ──
function exportFavs() {{
  const favs = getFavs();
  if (favs.length === 0) {{ showToast('没有收藏可导出'); return; }}
  const blob = new Blob([JSON.stringify(favs, null, 2)], {{ type: 'application/json' }});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'market_brief_favs_' + new Date().toISOString().slice(0,10) + '.json';
  a.click();
  URL.revokeObjectURL(url);
  showToast('已导出 ' + favs.length + ' 条收藏');
}}

// ── 导入收藏（合并去重）──
function importFavs() {{
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.json';
  input.onchange = function() {{
    const file = this.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function(e) {{
      try {{
        const incoming = JSON.parse(e.target.result);
        if (!Array.isArray(incoming)) throw new Error('格式错误');
        let existing = getFavs();
        const existingIds = new Set(existing.map(f => f.id));
        let added = 0;
        for (const f of incoming) {{
          if (!f.id || !f.title) continue;
          if (existingIds.has(f.id)) continue;
          // 丢弃 html 字段（兼容旧格式）
          existing.push({{
            id: f.id, sid: f.sid || '', date: f.date || '',
            session: f.session || '', title: f.title,
            saved_at: f.saved_at || new Date().toISOString()
          }});
          existingIds.add(f.id);
          added++;
        }}
        if (existing.length > MAX_FAVS) {{
          existing = existing.slice(existing.length - MAX_FAVS);
        }}
        saveFavs(existing);
        showToast('导入了 ' + added + ' 条，合并后共 ' + existing.length + ' 条');
      }} catch(err) {{
        showToast('导入失败：文件格式不正确');
      }}
    }};
    reader.readAsText(file);
  }};
  input.click();
}}

// ── 更新所有 ☆ 按钮状态 ──
function updateAllStarBtns() {{
  const favs = getFavs();
  const favIdSet = new Set(favs.map(f => f.id));
  document.querySelectorAll('.fav-btn').forEach(btn => {{
    const sid = btn.getAttribute('data-sid');
    const favId = makeFavId(sid);
    if (favIdSet.has(favId)) {{
      btn.textContent = '★';
      btn.classList.add('on');
    }} else {{
      btn.textContent = '☆';
      btn.classList.remove('on');
    }}
  }});
}}

// ── 收起/展开面板 ──
function toggleFavPanel() {{
  const list = document.getElementById('favList');
  const toggle = document.getElementById('favToggle');
  const actions = document.getElementById('favActions');
  if (list.style.display === 'none') {{
    list.style.display = '';
    if (actions) actions.style.display = '';
    toggle.textContent = '▾';
  }} else {{
    list.style.display = 'none';
    if (actions) actions.style.display = 'none';
    toggle.textContent = '▸';
  }}
}}

// ── Toast 提示 ──
function showToast(msg) {{
  const toast = document.getElementById('favToast');
  if (!toast) return;
  toast.textContent = msg;
  toast.classList.add('show');
  clearTimeout(toast._tid);
  toast._tid = setTimeout(() => toast.classList.remove('show'), 1500);
}}

// ── 初始化 ──
(function initFavs() {{
  renderFavPanel();
  updateAllStarBtns();
  // 处理跨页面锚点跳转
  const hash = window.location.hash;
  if (hash) {{
    const target = document.querySelector(`[data-section-id="${{hash.slice(1)}}"]`);
    if (target) {{
      setTimeout(() => {{
        target.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
        target.style.boxShadow = '0 0 0 3px var(--amber)';
        setTimeout(() => target.style.boxShadow = '', 2500);
      }}, 300);
    }}
  }}
}})();
</script>

</body>
</html>"""
    return html


# ============================================================
#  PDF 生成
# ============================================================

def cleanup_old_files(days=7, max_per_run=50):
    """清理旧的图表和 PDF 文件，避免 docs/ 目录膨胀导致 Pages 部署失败。

    通过文件名中的日期判断新旧（GitHub Actions checkout 不保留原始 mtime，
    所有签出文件的 mtime 都是 checkout 时间，用 mtime 判断不可靠）。

    保留逻辑：
      - charts/: 删除文件名中日期超过 `days` 天的 PNG 文件
      - pdf/:    删除文件名中日期超过 `days` 天的 PDF（保留 latest.pdf）
      - max_per_run: 单次最多删除数量，防止首次运行产生超大 commit
    """
    import glob as _glob
    import re as _re

    today = datetime.now()
    cutoff = today - timedelta(days=days)
    cutoff_date = cutoff.date()
    total_removed = 0
    skipped = 0

    # 先收集所有待删除文件，按日期从旧到新排序
    to_delete = []

    for subdir, pattern, date_re in [
        # charts:  000002_SZ_20260528.png → 2026-05-28
        ("charts", "*.png", r'_(\d{4})(\d{2})(\d{2})\.png$'),
        # pdf:     股市简报_2026-06-25_0020.pdf → 2026-06-25
        ("pdf", "股市简报_*.pdf", r'(\d{4}-\d{2}-\d{2})_\d{4}\.pdf$'),
    ]:
        dir_path = os.path.join("docs", subdir)
        if not os.path.isdir(dir_path):
            continue
        for fp in _glob.glob(os.path.join(dir_path, pattern)):
            filename = os.path.basename(fp)
            m = _re.search(date_re, filename)
            if not m:
                continue
            try:
                if '-' in m.group(1):
                    file_date = datetime.strptime(m.group(1), '%Y-%m-%d').date()
                else:
                    file_date = datetime.strptime(
                        m.group(1) + m.group(2) + m.group(3), '%Y%m%d'
                    ).date()
            except (ValueError, IndexError):
                continue

            if file_date < cutoff_date:
                to_delete.append((file_date, fp))

    # 从最旧的文件开始删，限制单次数量
    to_delete.sort(key=lambda x: x[0])
    for _, fp in to_delete[:max_per_run]:
        try:
            os.remove(fp)
            total_removed += 1
        except OSError:
            pass

    skipped = max(0, len(to_delete) - total_removed)

    if total_removed > 0:
        print(f"[Cleanup] 已清理 {total_removed} 个旧文件 (>{days}天, cutoff={cutoff_date})"
              + (f", 剩余 {skipped} 个将在后续运行中逐步清理" if skipped else ""))
    else:
        print(f"[Cleanup] 无需清理 (>{days}天, cutoff={cutoff_date})")


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

def deploy_github_pages(html_content, session_slug="am"):
    """将 HTML 写入 docs/ 目录，同时生成主文件和场次文件。"""
    today = datetime.now().strftime("%Y%m%d")
    os.makedirs("docs", exist_ok=True)

    # 主文件（最新报告，向后兼容）
    report_path = f"docs/report_{today}.html"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    # 场次文件（早报/晚报独立保存，不被覆盖）
    session_path = f"docs/report_{today}_{session_slug}.html"
    with open(session_path, "w", encoding="utf-8") as f:
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
#  主流程
# ============================================================

def main():
    session_label, session_slug = get_session_label()
    print(f"[{datetime.now()}] 开始生成每日市场情报（{session_label}）...")
    print()

    # 1. 行情
    print("▸ 抓取指数行情...")
    quotes = fetch_index_quotes()
    for q in quotes:
        print(f"  {q['name']}: {q['price']} ({q['change']})")

    # 2. 新闻 + 资金面
    print("\n▸ 抓取多市场新闻 & 资金面数据...")
    news_list, fund_flow = fetch_all_news()

    a_news = [n for n in news_list if n.get("market") == "A股" and "error" not in n]
    us_news = [n for n in news_list if n.get("market") == "美股" and "error" not in n]
    hk_news = [n for n in news_list if n.get("market") == "港股" and "error" not in n]
    print(f"  A股: {len(a_news)}条 | 美股: {len(us_news)}条 | 港股: {len(hk_news)}条")
    if fund_flow:
        latest = fund_flow[-1]
        direction = "流入" if latest["net_flow"] >= 0 else "流出"
        print(f"  主力资金({latest['date']}): {direction} {abs(latest['net_flow']):.1f}亿")

    if not a_news and not us_news and not hk_news:
        print("没有抓取到任何新闻，退出")
        return

    # 3. 格式化 & LLM 分析
    news_text = format_news(news_list, fund_flow)
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

    # 5. 清理旧文件（防止 docs/ 膨胀导致 Pages 部署失败）
    cleanup_old_files(days=7)

    # 6. 技术分析图表
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
                                fund_flow, session_label=session_label, session_slug=session_slug)
    page_url = deploy_github_pages(html, session_slug=session_slug)

    # 7. PDF
    print("▸ 生成 PDF...")
    html_file = f"docs/report_{today_str}.html"
    generate_pdf(html_file)

    # 8. 保存 Markdown
    report_file = f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(f"# 每日市场情报 - {datetime.now().strftime('%Y-%m-%d')} {session_label}\n\n")
        f.write(report)
    print(f"Markdown 报告: {report_file}")

    # 9. GitHub Actions output
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"report_file={report_file}\n")
            f.write(f"page_url={page_url}\n")

    print(f"\n[{datetime.now()}] 完成")


if __name__ == "__main__":
    main()
