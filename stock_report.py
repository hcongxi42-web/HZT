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
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from utils import beijing_now

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

def _set_github_output(key, value):
    """写入 GitHub Actions output 变量（本地运行时无操作）。"""
    try:
        output_file = os.environ.get("GITHUB_OUTPUT", "")
        if output_file:
            with open(output_file, "a") as f:
                f.write(f"{key}={value}\n")
    except Exception:
        pass

# ============================================================
#  UP主观点 — 配置 & 文件扫描
# ============================================================

def _load_up_config():
    """加载 UP主 配置文件，文件不存在时返回空 dict。"""
    import os as _os
    config_path = _os.path.join("up主的每日观点", "up_config.json")
    if not _os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# 模块级缓存
UP_CONFIG = _load_up_config()


def find_today_opinions(opinion_dir="up主的每日观点", date_offset=0):
    """扫描 UP主观点目录（含子目录），返回指定日期的转录文件列表。

    Args:
        opinion_dir: 观点文件根目录
        date_offset: 日期偏移量，0=今天，-1=昨天（早报用），1=明天

    支持两种组织方式：
      - 扁平：up主的每日观点/xxx.ai-zh.txt
      - 分类：up主的每日观点/up擒龙先生/xxx.ai-zh.txt

    Returns:
        list[dict]: [{up_id, name, platform, filename, content, char_count}, ...]
        无今日文件时返回 []
    """
    import glob as _glob

    if not os.path.isdir(opinion_dir):
        return []

    target = beijing_now() + timedelta(days=date_offset)
    target_month = target.month
    target_day = target.day

    results = []
    # 递归扫描目录和子目录
    for fp in _glob.glob(os.path.join(opinion_dir, "**", "*.txt"), recursive=True):
        fname = os.path.basename(fp)
        parent_dir = os.path.basename(os.path.dirname(fp))

        # 从文件名解析日期（支持 "7月6日" / "7月6号" 两种中文格式）
        date_match = re.search(r'(\d{1,2})月(\d{1,2})[日号]', fname)
        if date_match:
            file_month = int(date_match.group(1))
            file_day = int(date_match.group(2))
            if file_month != target_month or file_day != target_day:
                print(f"  ⏭ 跳过非目标日期文件: {parent_dir}/{fname} (需要 {target_month}月{target_day}日)")
                continue  # 不是目标日期的文件，跳过
        else:
            # 文件名不含日期，兜底通过（可能是手动放的未命名文件）
            print(f"  ⚐ 文件名未识别日期，兜底通过: {parent_dir}/{fname}")

        # 从文件名解析 UP主 ID（长数字，通常在文件名靠后位置）
        id_match = re.search(r'\.(\d{8,20})(?:\.ai-zh)?\.txt$', fname)
        up_id = id_match.group(1) if id_match else ""

        # 读取内容
        try:
            with open(fp, "r", encoding="utf-8") as f:
                content = f.read()
            if not content or len(content.strip()) < 50:
                print(f"  ⚠ 跳过空/过短文件: {fname}")
                continue
        except Exception as e:
            print(f"  ⚠ 无法读取 {fname}: {e}")
            continue

        # 查找 UP主 显示名称
        cfg = UP_CONFIG.get(up_id, {})
        name = cfg.get("name", "")
        if not name:
            # 尝试从父目录名推断（如 "up擒龙先生" → "擒龙先生"）
            if parent_dir and parent_dir != opinion_dir and parent_dir.startswith("up"):
                name = parent_dir[2:]  # 去掉 "up" 前缀
            else:
                name = f"UP主{up_id}" if up_id else fname[:20]
        platform = cfg.get("platform", "")

        # 判断类型：目录名含「信息差」→ 信息类，其他 → 观点类
        kind = "info" if "信息差" in parent_dir else "opinion"

        results.append({
            "up_id": up_id,
            "name": name,
            "platform": platform,
            "kind": kind,
            "filename": fname,
            "content": content,
            "char_count": len(content),
        })

    if results:
        print(f"  ✓ 匹配到 {len(results)} 个文件: {', '.join(r['name'] + '/' + r['filename'] for r in results)}")

    return results


def get_session_label():
    """根据北京时间判断报告场次（A股交易时段）。"""
    hour = beijing_now().hour
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
    today_str = beijing_now().strftime("%Y-%m-%d")

    lines = [
        f"日期: {beijing_now().strftime('%Y-%m-%d %H:%M')}",
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
#  LLM 分析 — 提示词（从 prompts/ 目录加载，方便独立调整）
# ============================================================

def _load_prompt(name):
    """从 prompts/ 目录加载提示词模板。"""
    prompt_path = os.path.join("prompts", name)
    if not os.path.exists(prompt_path):
        print(f"[WARN] 提示词文件不存在: {prompt_path}，使用内置默认值")
        return ""
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()

SYSTEM_PROMPT = _load_prompt("system_analyst.txt")
USER_PROMPT_TEMPLATE = _load_prompt("user_analyst.txt")
STOCK_PICKER_SYSTEM_PROMPT = _load_prompt("system_stock_picker.txt")
STOCK_PICKER_TEMPLATE = _load_prompt("user_stock_picker.txt")
OPINION_SYSTEM_PROMPT = _load_prompt("system_opinion.txt")
OPINION_USER_PROMPT_TEMPLATE = _load_prompt("user_opinion.txt")
INFO_GAP_SYSTEM_PROMPT = _load_prompt("system_info_gap.txt")
INFO_GAP_USER_PROMPT_TEMPLATE = _load_prompt("user_info_gap.txt")

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
        "model": "deepseek-v4-flash",
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
        content = data["choices"][0]["message"]["content"]
        # 偶发返回空内容（如并行调用触发的瞬时限流），视为失败以便重试/降级
        if not content or not content.strip():
            return "API 调用失败: 返回内容为空"
        return content
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return f"API 调用失败: HTTP {e.code} - {body[:300]}"
    except Exception as e:
        return f"API 调用失败: {str(e)}"


def _call_deepseek_safe(system_prompt, user_prompt, temperature=0.5, max_tokens=4096, section_name="AI分析"):
    """带优雅降级的 DeepSeek API 调用。失败时返回友好提示而非原始错误文本。"""
    result = _call_deepseek(system_prompt, user_prompt, temperature, max_tokens)
    # 空返回多为并行调用下的瞬时限流，短暂等待后重试一次
    if result == "API 调用失败: 返回内容为空":
        print(f"[LLM] {section_name} 返回空内容，3 秒后重试一次...")
        time.sleep(3)
        result = _call_deepseek(system_prompt, user_prompt, temperature, max_tokens)
    if result.startswith("错误") or result.startswith("API 调用失败"):
        print(f"[LLM] {section_name} 调用失败，使用降级: {result[:100]}")
        return f"*({section_name}暂时不可用，请稍后重试)*"
    return result


def call_llm(news_text):
    """调用 LLM 生成市场分析报告，并清理 #### / *** 标记。"""
    raw = _call_deepseek_safe(SYSTEM_PROMPT, USER_PROMPT_TEMPLATE.format(news_text=news_text),
                              temperature=0.5, max_tokens=4096, section_name="市场分析")
    return _cleanup_report(raw)


def call_stock_picker(news_text, opinion_context="", info_context=""):
    """调用 LLM 执行产业链选股分析（融合新闻+UP主观点+信息差），并清洗输出（含去 **）。"""
    raw = _call_deepseek_safe(STOCK_PICKER_SYSTEM_PROMPT,
                              STOCK_PICKER_TEMPLATE.format(
                                  news_text=news_text,
                                  opinion_context=opinion_context,
                                  info_context=info_context,
                              ),
                              temperature=0.3, max_tokens=6144, section_name="AI选股")
    return _cleanup_report(raw, strip_bold=True)


def format_stock_picks(picks_md):
    """将选股结果封装为日报板块。"""
    if not picks_md or not picks_md.strip():
        return ""
    return (
        "\n\n---\n\n"
        "## AI选股\n\n"
        f"{picks_md}\n"
    )


def call_opinion_analyzer(opinion_text):
    """调用 LLM 分析UP主财经观点，返回结构化 markdown。"""
    raw = _call_deepseek_safe(
        OPINION_SYSTEM_PROMPT,
        OPINION_USER_PROMPT_TEMPLATE.format(opinion_text=opinion_text),
        temperature=0.4,
        max_tokens=4096,
        section_name="UP主观点蒸馏",
    )
    return _cleanup_report(raw)


def call_info_analyzer(info_text):
    """调用 LLM 提炼信息差，提取核心事实（不做多空判断）。"""
    raw = _call_deepseek_safe(
        INFO_GAP_SYSTEM_PROMPT,
        INFO_GAP_USER_PROMPT_TEMPLATE.format(info_text=info_text),
        temperature=0.3,
        max_tokens=3072,
        section_name="信息差提炼",
    )
    return _cleanup_report(raw)


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
        r'\n*###\s*\s*技术分析图\s*\n(?:\*\*.*?\*\*[^\n]*\n|!\[.*?\]\(.*?\)\n|\n)*',
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
                chart_md_lines.append('<span style="color:#d63031;font-weight:600;"> 注：K线图仅保留近7天，历史图表将自动清理。如需长期保存，请右键另存为。</span>')
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
                         fund_flow=None, session_label="早报", session_slug="am",
                         opinion_html="", opinion_title="今日收盘UP主观点"):
    """生成 Bloomberg Terminal 风格 HTML 详情页。"""
    bj_now = beijing_now()
    today = bj_now.strftime("%Y-%m-%d")
    today_en = bj_now.strftime("%B %d, %Y")
    now_str = bj_now.strftime("%H:%M")
    update_datetime = bj_now.strftime("%Y-%m-%d %H:%M")  # 完整时间戳，页面展示用
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[bj_now.weekday()]
    wk_cn = weekday_cn[bj_now.weekday()]
    fav_date_key = f"{today}_{session_slug}"  # 用于收藏唯一标识

    # ---- 历史简报导航 ----
    history_links_html = ""
    if page_base_url:
        for i in range(1, 6):
            d = bj_now - timedelta(days=i)
            label = d.strftime("%m月%d日")
            history_links_html += (
                f'<a class="hl" href="{page_base_url}report_{d.strftime("%Y%m%d")}.html">'
                f'{label}</a>'
            )
        # 日期选择器 + 早报/晚报切换按钮
        today_str = bj_now.strftime("%Y-%m-%d")
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
        # 计算 5 日汇总
        total_flow = sum(r["net_flow"] for r in fund_flow)
        total_color = "#00c853" if total_flow >= 0 else "#ff1744"
        total_label = "累计流入" if total_flow >= 0 else "累计流出"

        fund_panel += (
            '<div class="fp">'
            '<div class="fp-h">'
            f'主力资金流向（近5日 · 亿元）'
            f'<span class="fp-total" style="color:{total_color}">'
            f'{total_label} {total_flow:+.1f} 亿'
            f'</span>'
            f'</div>'
            f'<div class="fp-bars">'
        )
        max_val = max(abs(r["net_flow"]) for r in fund_flow) if fund_flow else 1
        for row in fund_flow:
            net = row["net_flow"]
            is_pos = net >= 0
            pct = min(abs(net) / max_val * 100, 100) if max_val else 0
            color = "#00c853" if is_pos else "#ff1744"
            bar_class = "fp-bar-fill in" if is_pos else "fp-bar-fill out"
            fund_panel += (
                f'<div class="fp-bar-row">'
                f'<span class="fp-date">{row["date"][-5:]}</span>'
                f'<span class="fp-bar-bg">'
                f'<span class="{bar_class}" style="width:{pct}%;background:{color}"></span>'
                f'</span>'
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

    # ---- 观点蒸馏板块 ----
    if opinion_html:
        opinion_section = (
            f'  <div class="sec-hdr" style="margin-top:40px;">{opinion_title}</div>\n'
            f'  <div class="report-body opinions-body">{opinion_html}</div>\n'
        )
    else:
        opinion_section = ""

    # ---- 组装 ----
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MARKET BRIEF · {today} {session_label}</title>
<link rel="stylesheet" href="{page_base_url}style.css?v={today}">
</head>
<body>

<!-- TOP BAR -->
<div class="topbar">
  <div class="topbar-inner">
    <div class="logo">MARKET<em>//</em>BRIEF</div>
    <div class="topbar-meta">{weekday}<br>{now_str} CST（北京时间）</div>
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
  <div class="masthead-update">更新时间：{update_datetime}（北京时间 CST）</div>
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

  {opinion_section}
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
      <span> 自选新闻 · <em id="favCount">0</em></span>
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
window.MB_CONFIG = {{
  fav_date_key: '{fav_date_key}',
  today: '{today}',
  session_label: '{session_label}',
  session_slug: '{session_slug}',
  page_base_url: '{page_base_url}'
}};
</script>
<script src="{page_base_url}script.js?v={today}" defer></script>

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

    today = beijing_now()
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

    now = beijing_now()
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
    today = beijing_now().strftime("%Y%m%d")
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

    # 生成归档索引页面
    generate_archive_page()

    return full_url


def generate_archive_page():
    """扫描 docs/ 下所有报告文件，生成按月份分组的归档索引页面。"""
    import glob as _glob

    archive_dir = "docs"
    if not os.path.isdir(archive_dir):
        return

    # 收集所有报告文件（含场次后缀）
    report_files = _glob.glob(os.path.join(archive_dir, "report_*.html"))
    # 按日期分组：{(date_str, session): path}
    entries = []
    for fp in report_files:
        fname = os.path.basename(fp)
        # 匹配 report_YYYYMMDD.html 或 report_YYYYMMDD_am.html / report_YYYYMMDD_pm.html
        m = re.match(r'report_(\d{8})(?:_(am|pm))?\.html', fname)
        if not m:
            continue
        date_str = m.group(1)  # YYYYMMDD
        session = m.group(2) or ""  # am/pm or empty (full day)
        try:
            year = date_str[:4]
            month = date_str[4:6]
            day = date_str[6:8]
            display_date = f"{year}-{month}-{day}"
        except (IndexError, ValueError):
            continue
        entries.append({
            "date_str": date_str,
            "display_date": display_date,
            "year": year,
            "month": month,
            "day": day,
            "session": session,
            "filename": fname,
            "label": "早报" if session == "am" else ("晚报" if session == "pm" else "全天"),
        })

    if not entries:
        return

    # 按日期倒序排列
    entries.sort(key=lambda e: (e["date_str"], e["session"]), reverse=True)

    # 构建 HTML
    base_url = os.environ.get("GITHUB_REPOSITORY", "")
    if base_url:
        owner = base_url.split("/")[0].lower()
        repo_name = base_url.split("/")[1]
        page_base = f"https://{owner}.github.io/{repo_name}/"
    else:
        page_base = "./"

    rows_html = []
    current_month = ""
    for e in entries:
        month_label = f"{e['year']}年{e['month']}月"
        if month_label != current_month:
            current_month = month_label
            rows_html.append(f'<tr class="mo-sep"><td colspan="3">{current_month}</td></tr>')

        weekday_cn = ["一", "二", "三", "四", "五", "六", "日"]
        try:
            from datetime import date
            wd = date(int(e["year"]), int(e["month"]), int(e["day"])).weekday()
            wd_label = weekday_cn[wd]
        except Exception:
            wd_label = ""

        rows_html.append(
            f'<tr>'
            f'<td class="ad">{e["display_date"]} <span class="aw">周{wd_label}</span></td>'
            f'<td class="as">{e["label"]}</td>'
            f'<td><a href="{page_base}{e["filename"]}">查看报告 →</a></td>'
            f'</tr>'
        )

    archive_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MARKET BRIEF · 报告归档</title>
<link rel="stylesheet" href="{page_base}style.css?v=archive">
<style>
  body {{ background: var(--bg); font-family: 'Inter','Noto Sans SC',sans-serif; }}
  .archive-wrap {{ max-width: 800px; margin: 40px auto; padding: 0 24px; }}
  .archive-wrap h1 {{ font-size: 22px; margin-bottom: 6px; }}
  .archive-wrap .sub {{ color: var(--text-muted); font-size: 13px; margin-bottom: 32px; }}
  .archive-wrap table {{ width: 100%; border-collapse: collapse; }}
  .archive-wrap td {{ padding: 8px 12px; border-bottom: 1px solid var(--border-light); font-size: 14px; }}
  .archive-wrap td.ad {{ font-family: 'JetBrains Mono',monospace; font-size: 13px; }}
  .archive-wrap td.as {{ color: var(--text-secondary); font-size: 12px; }}
  .archive-wrap td a {{ color: var(--accent-blue); text-decoration: none; }}
  .archive-wrap td a:hover {{ text-decoration: underline; }}
  .archive-wrap .mo-sep td {{ background: var(--bg-elevated); font-weight: 700; font-size: 13px; padding: 12px; color: var(--accent); }}
  .archive-wrap .aw {{ color: var(--text-muted); font-size: 11px; margin-left: 6px; }}
  .back-link {{ display: inline-block; margin-bottom: 24px; font-size: 13px; color: var(--accent-blue); text-decoration: none; }}
  .back-link:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="archive-wrap">
  <a class="back-link" href="{page_base}">← 返回最新报告</a>
  <h1>报告归档</h1>
  <p class="sub">{len(entries)} 份报告 · 最后更新 {entries[0]['display_date'] if entries else ''}</p>
  <table>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
</div>
</body>
</html>"""

    archive_path = os.path.join(archive_dir, "archive.html")
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(archive_html)
    print(f"归档页面已生成: {archive_path}（{len(entries)} 份报告）")


# ============================================================
#  主流程
# ============================================================

def main():
    session_label, session_slug = get_session_label()
    print(f"[{beijing_now()}] 开始生成每日市场情报（{session_label}）...")
    print()

    # 周末判断：周六全天不更新，周日只更新晚报
    wd = beijing_now().weekday()  # 0=Mon ... 5=Sat 6=Sun
    if wd == 5:  # 周六
        print("⏭ 周六不更新，退出")
        _set_github_output("skip", "1")
        return
    if wd == 6 and session_label == "早报":  # 周日早报不更新
        print("⏭ 周日不更新早报，退出")
        _set_github_output("skip", "1")
        return

    # 防重复：自动 cron 触发时，如本场次报告已存在则跳过（手动触发不受限制）
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "schedule":
        today_str = beijing_now().strftime("%Y%m%d")
        existing_report = f"docs/report_{today_str}_{session_slug}.html"
        if os.path.exists(existing_report):
            print(f"⏭ 本场次报告已存在（{existing_report}），跳过重复生成")
            print("  （如确需重新生成，请通过 Actions 页面手动触发 workflow_dispatch）")
            _set_github_output("skip", "1")
            return

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

    # 4. 财经观点蒸馏 + 信息差 — 先扫描UP主文件（选股需用到观点上下文）
    if session_label == "早报":
        opinion_title = "昨日收盘UP主观点"
        opinion_md_title = "## 昨日收盘UP主观点"
        date_offset = -1
    else:
        opinion_title = "今日收盘UP主观点"
        opinion_md_title = "## 今日收盘UP主观点"
        date_offset = 0

    print(f"\n▸ 扫描UP主观点文件（{opinion_title}）...")
    all_files = find_today_opinions(date_offset=date_offset)
    opinion_files = [o for o in all_files if o["kind"] == "opinion"]
    info_files = [o for o in all_files if o["kind"] == "info"]

    opinion_context = ""   # UP主蒸馏结果，传给选股
    info_context = ""      # 信息差原始内容，传给选股
    opinion_md = ""        # UP主蒸馏 markdown，追加到 report
    info_md = ""           # 信息差提炼结果

    # 4a. 预处理：组装 UP主观点文本
    combined_text = ""
    if opinion_files:
        print(f"  发现 {len(opinion_files)} 位UP主观点")
        parts = []
        for o in opinion_files:
            parts.append(f"【UP主: {o['name']}（{o['filename']}，{o['char_count']}字）】\n{o['content']}")
        combined_text = "\n\n---\n\n".join(parts)
    else:
        print("  未找到UP主观点文件")

    # 4b. 预处理：组装信息差文本
    info_raw = ""
    if info_files:
        print(f"  发现 {len(info_files)} 条信息差")
        info_parts = []
        for o in info_files:
            info_parts.append(f"【{o['name']}（{o['filename']}，{o['char_count']}字）】\n{o['content']}")
            print(f"    ✓ {o['name']}: {o['char_count']}字")
        info_raw = "\n\n---\n\n".join(info_parts)

    # 4c. 并行调用 LLM：观点蒸馏 + 信息差提炼（二者独立，无依赖）
    if combined_text or info_raw:
        print("  开始并行 AI 分析（观点蒸馏 + 信息差提炼）...")
        with ThreadPoolExecutor(max_workers=2) as executor:
            opinion_future = executor.submit(call_opinion_analyzer, combined_text) if combined_text else None
            info_future = executor.submit(call_info_analyzer, info_raw) if info_raw else None

            if opinion_future:
                opinion_md = opinion_future.result()
                if opinion_md and "暂时不可用" not in opinion_md:
                    report += f"\n\n---\n\n{opinion_md_title}\n\n{opinion_md}"
                    opinion_context = f"## UP主市场观点（AI蒸馏）\n\n{opinion_md}"
                    print("  观点蒸馏完成")
                else:
                    print(f"  观点分析失败: {opinion_md[:100] if opinion_md else '无返回'}")
                    opinion_md = ""
                    # 降级：直接展示原始观点文本，避免内容丢失
                    if combined_text:
                        opinion_context = f"## UP主市场观点（原文）\n\n{combined_text}"
                        report += f"\n\n---\n\n{opinion_md_title}\n\n{combined_text}"
                        print("  已降级展示UP主观点原文")

            if info_future:
                info_md = info_future.result()
                if info_md and "暂时不可用" not in info_md:
                    info_context = f"## 信息差提炼（AI 提取关键事实）\n\n{info_md}"
                    report += f"\n\n---\n\n## 信息差提炼\n\n{info_md}"
                    print("  信息差提炼完成")
                else:
                    print(f"  信息差提炼失败: {info_md[:100] if info_md else '无返回'}")
                    info_context = f"## 信息差补充\n\n{info_raw}"
                    report += f"\n\n---\n\n## 信息差补充\n\n{info_raw}"

    # 5. AI 选股 — 融合新闻 + UP主观点 + 信息差
    print("\n▸ 执行 AI 产业链选股（融合新闻+观点+信息差）...")
    stock_picks = call_stock_picker(news_text,
                                    opinion_context=opinion_context,
                                    info_context=info_context)

    # 预先构造 GitHub Pages URL
    today_str = beijing_now().strftime("%Y%m%d")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        owner = repo.split("/")[0].lower()
        repo_name = repo.split("/")[1]
        page_base_url = f"https://{owner}.github.io/{repo_name}/"
    else:
        page_base_url = "https://hcongxi42-web.github.io/HZT/"

    # 6. 清理旧文件
    cleanup_old_files(days=7)

    # 7. 技术分析图表
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

    # 组装完整报告 — 产业链选股放在观点蒸馏后面
    if stock_picks:
        report += format_stock_picks(stock_picks)
        print("  AI 选股完成")

    print("\n" + "=" * 60)
    print(report[:2000])
    if len(report) > 2000:
        print(f"... (总 {len(report)} 字符)")
    print("=" * 60)

    # 8. HTML
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
    report_file = f"report_{beijing_now().strftime('%Y%m%d_%H%M')}.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(f"# 每日市场情报 - {beijing_now().strftime('%Y-%m-%d')} {session_label}\n\n")
        f.write(report)
    print(f"Markdown 报告: {report_file}")

    # 9. GitHub Actions output
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"report_file={report_file}\n")
            f.write(f"page_url={page_url}\n")

    print(f"\n[{beijing_now()}] 完成")


if __name__ == "__main__":
    main()
