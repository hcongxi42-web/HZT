"""
独立版股市日报生成器 - 用于 GitHub Actions 定时运行
多市场新闻聚合 + DeepSeek AI 分析 + 技术图表 + GitHub Pages 部署
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from utils import beijing_now


# 共享新闻 & 资金面抓取模块
from news_fetcher import fetch_all_news_flat

# 项目根目录（stock_report.py 所在目录）。所有文件路径锚定到此处，
# 修复：从非仓库根目录启动时 prompts/ 静默为空、docs/ 写错位置的隐患。
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Windows 下 stdout/stderr 被重定向（写日志、管道、被外部工具捕获）时编码会退化为 GBK，
# 日志里的 ⚠️ / ⏭ / ▸ 等字符会抛 UnicodeEncodeError，直接中断整条流水线。
# 这里只把编码错误策略改成 replace（无法编码的字符变 "?"），不影响正常中文输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


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
    config_path = os.path.join(_BASE_DIR, "up主的每日观点", "up_config.json")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# 模块级缓存
UP_CONFIG = _load_up_config()


def find_today_opinions(opinion_dir=None, date_offset=0, base_date=None):
    """扫描 UP主观点目录（含子目录），返回指定日期的转录文件列表。

    Args:
        opinion_dir: 观点文件根目录
        date_offset: 日期偏移量，0=当天，-1=前一天（早报用），1=后一天
        base_date: 基准时间，默认取当前北京时间；延迟运行时传"报告归属时间"，
                   避免跨午夜后取错日期的转录文件

    支持两种组织方式：
      - 扁平：up主的每日观点/xxx.ai-zh.txt
      - 分类：up主的每日观点/up擒龙先生/xxx.ai-zh.txt

    Returns:
        list[dict]: [{up_id, name, platform, filename, content, char_count}, ...]
        无今日文件时返回 []
    """
    import glob as _glob

    if opinion_dir is None:
        opinion_dir = os.path.join(_BASE_DIR, "up主的每日观点")
    if not os.path.isdir(opinion_dir):
        return []

    target = (base_date or beijing_now()) + timedelta(days=date_offset)
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


# 定时任务 → 场次映射。必须按【触发本子的 cron】判定，不能按实际执行时刻：
# GitHub Actions 定时任务常延迟数小时，22:17 的晚报一旦跨过午夜才执行，
# 按小时判断就会误判为早报（文件名也写到次日 _am，导致晚报丢失、被早报顶掉）。
CRON_SESSIONS = {
    "17 14 * * *": ("晚报", "pm"),   # 北京时间 22:17
    "17 23 * * *": ("早报", "am"),   # 北京时间次日 07:17
}


def get_session_label(now=None, schedule=None):
    """判断本次报告场次，返回 (中文标签, 文件名后缀)。

    优先级：
      1) 定时触发 → 按触发用的 cron 精确映射（不受调度延迟影响）；
      2) 手动触发 / 未知 cron / 本地运行 → 退回按小时判断（原行为）。
    """
    if schedule is None:
        schedule = os.environ.get("GITHUB_EVENT_SCHEDULE", "")
    key = " ".join(str(schedule).split())
    if key in CRON_SESSIONS:
        return CRON_SESSIONS[key]

    now = now or beijing_now()
    return ("早报", "am") if now.hour < 12 else ("晚报", "pm")


def get_report_time(session_slug, now=None):
    """本次报告【归属的发布时间】。

    晚报纸定为 22:17：若实际执行已跨过午夜（0–11 点），归属时间回退一天，
    保证晚报写进"当天"的 _pm 文件，而不是次日的 _am 文件。
    """
    now = now or beijing_now()
    if session_slug == "pm" and now.hour < 12:
        return now - timedelta(days=1)
    return now


# ============================================================
#  交易日判断 & 运行摘要
# ============================================================

MARKET_HOLIDAYS_FILE = os.path.join(_BASE_DIR, "holidays.json")


def load_market_holidays():
    """读取法定休市日表 holidays.json，失败返回空集合。

    格式: {"holidays": ["2026-10-01", ...]}
    只填**工作日**休市日（周六周日由程序自动跳过，无需重复登记）。
    """
    if not os.path.exists(MARKET_HOLIDAYS_FILE):
        return set()
    try:
        with open(MARKET_HOLIDAYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("holidays", []) if isinstance(data, dict) else list(data)
        return {d.strip() for d in items if isinstance(d, str) and d.strip()}
    except Exception as e:
        print(f"[WARN] holidays.json 解析失败，本次忽略节假日表: {e}")
        return set()


def get_skip_reason(quotes=None, session_label="晚报", now=None, holidays=None):
    """统一判断本次运行是否应跳过（纯逻辑，便于回归测试）。

    返回原因字符串；空串表示正常出报。规则自上而下：
      1) 周六全天不更新；周日仅跳过早报（周日晚报是"周末版"，保留）；
      2) 命中 holidays.json 的法定休市日 → 早报/晚报都跳过（解决早报拿不到当天行情、
         无法自证的缺口）；
      3) 晚报且在**工作日**：行情自带的最新交易日 ≠ 今天 → 判定休市（数据自证，
         可兜住节假日表漏配的情况）。周末不做此判断，否则周日晚报会被误杀。
         网络异常导致 trade_date 缺失时不拦截，保证正常出报。
    """
    now = now or beijing_now()
    wd = now.weekday()  # 0=Mon ... 5=Sat 6=Sun

    if wd == 5:
        return "周六不更新"
    if wd == 6 and session_label == "早报":
        return "周日不更新早报"

    today_date = now.strftime("%Y-%m-%d")
    if holidays is None:
        holidays = load_market_holidays()
    if today_date in holidays:
        return f"今日为法定休市日（{today_date}）"

    if session_label == "晚报" and wd < 5 and quotes:
        trade_date = next((q.get("trade_date") for q in quotes if q.get("trade_date")), "")
        if trade_date and trade_date != today_date:
            return f"今日 A 股休市（行情最新交易日 {trade_date} ≠ 今天 {today_date}）"
    return ""


def write_run_summary(rows, title="本次运行体检单"):
    """输出运行摘要：stdout + GitHub Actions 运行页摘要（GITHUB_STEP_SUMMARY）。

    rows: [(项目, 状态, 备注), ...]
    目的：某节降级/失败不再只藏在几百行日志里，Actions 页面一眼可见。
    """
    lines = [f"## {title}", "", "| 项目 | 状态 | 备注 |", "|------|------|------|"]
    for name, status, note in rows:
        lines.append(f"| {name} | {status} | {note or '—'} |")

    print("\n" + "-" * 60)
    for name, status, note in rows:
        print(f"  {name}: {status}" + (f"（{note}）" if note else ""))
    print("-" * 60)

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            print(f"[WARN] 写入 Actions 摘要失败: {e}")


def _abort_run(reason):
    """统一的"跳过本次运行"出口：打印 + 标记 skip + 摘要页留痕。"""
    print(f"⏭ {reason}")
    _set_github_output("skip", "1")
    write_run_summary([("本次运行", "跳过", reason)])


# ============================================================
#  指数行情抓取
# ============================================================

def _parse_sina_quote(code, name, parts):
    """解析新浪行情字段为标准结构（纯函数，便于回归测试）。

    字段实测（2026-09）:
      - A股指数 sh/sz: [名称,今开,昨收,现价,最高,最低, ..., parts[30]=交易日, parts[31]=时间]
      - 国际指数 int_: 仅 4 字段 [名称, 价格, 涨跌额, 涨跌幅]
    """
    if code.startswith("sh") or code.startswith("sz"):
        price = float(parts[3])
        prev_close = float(parts[2])
        high = float(parts[4])
        low = float(parts[5])
        change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
        return {
            "name": name, "code": code,
            "price": f"{price:.2f}",
            "change": f"{change_pct:+.2f}%",
            "high": f"{high:.2f}", "low": f"{low:.2f}",
            "trade_date": parts[30] if len(parts) > 30 else "",
        }
    if code.startswith("int_"):
        price = float(parts[1])
        change_pct = float(parts[3]) if len(parts) > 3 else 0
        return {
            "name": name, "code": code,
            "price": f"{price:.2f}",
            "change": f"{change_pct:+.2f}%",
            "high": "--", "low": "--",
            "trade_date": "",
        }
    raise ValueError(f"未知行情代码: {code}")


def fetch_index_quotes():
    """从新浪财经抓取 A 股主要指数 + 恒生 + 纳斯达克 实时行情。

    返回项含 trade_date（A 股指数的最新交易日，用于休市判断）。
    """
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
            results.append(_parse_sina_quote(code, name, parts))
        except Exception:
            results.append({"name": name, "code": code, "price": "--", "change": "--",
                            "high": "--", "low": "--", "trade_date": ""})
    return results


# ============================================================
#  市场情绪硬指标（涨跌家数 / 成交额 / 涨跌停 / 板块涨跌榜）
#  目的：让"大盘情绪"有数据支撑，而不是让模型拿新闻语气去猜。
# ============================================================

_EM_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_EM_REF = "https://quote.eastmoney.com/"


def _em_get_json(url, referer=_EM_REF, timeout=12, retries=2):
    """轻量 GET+JSON：失败退避重试，最终失败返回 None（调用方优雅降级，绝不编数据）。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _EM_UA, "Referer": referer})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    print(f"  [WARN] 情绪指标接口失败（已省略该项）: {last_err}")
    return None


def parse_breadth_payload(data):
    """解析 push2 ulist.np 返回 → 涨跌家数 / 两市成交额 / 各指数涨跌幅（纯函数）。

    字段实测（2026-09-10）: f104=上涨家数 f105=下跌家数 f106=平盘家数 f6=成交额(元)。
    """
    diff = ((data or {}).get("data") or {}).get("diff") or []
    up = down = flat = 0
    turnover_yi = 0.0
    indexes = []
    for it in diff:
        if not isinstance(it, dict):
            continue
        for key, acc in (("f104", "up"), ("f105", "down"), ("f106", "flat")):
            v = it.get(key)
            if isinstance(v, (int, float)):
                if acc == "up":
                    up += int(v)
                elif acc == "down":
                    down += int(v)
                else:
                    flat += int(v)
        amt = it.get("f6")
        if isinstance(amt, (int, float)):
            turnover_yi += amt / 1e8
        if isinstance(it.get("f3"), (int, float)):
            indexes.append((str(it.get("f14", "")), float(it["f3"])))
    if not indexes:
        return None
    return {"up": up, "down": down, "flat": flat,
            "turnover_yi": round(turnover_yi, 1), "indexes": indexes}


def parse_pool_count(data):
    """解析涨停池/跌停池返回 → 家数（纯函数；实测 data.tc）。"""
    tc = ((data or {}).get("data") or {}).get("tc")
    return int(tc) if isinstance(tc, (int, float)) else None


def parse_sector_list(data, limit=5):
    """解析 clist 板块榜 → [(板块名, 涨跌幅%)]（纯函数）。"""
    diff = ((data or {}).get("data") or {}).get("diff") or []
    out = []
    for it in diff:
        if not isinstance(it, dict):
            continue
        name, chg = it.get("f14"), it.get("f3")
        if name and isinstance(chg, (int, float)):
            out.append((str(name), float(chg)))
        if len(out) >= limit:
            break
    return out


def fetch_market_sentiment(trade_date=""):
    """抓取市场情绪硬指标：涨跌家数、两市成交额、涨跌停家数、板块涨跌榜。

    trade_date: "YYYYMMDD"。涨停/跌停池必须传【最近交易日】（早报在开盘前运行，
                当天尚无池数据，传当天会得到 0 家这种假数据）。
    任一子项失败 → 该项缺失、报告中对应行省略；全部失败 → 返回 {}。
    """
    sentiment = {}

    breadth = parse_breadth_payload(_em_get_json(
        "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
        "&secids=1.000001,0.399001&fields=f12,f14,f3,f6,f104,f105,f106"))
    if breadth:
        sentiment["breadth"] = breadth

    if trade_date:
        zt = parse_pool_count(_em_get_json(
            "https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
            f"&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fbt:asc&date={trade_date}"))
        dt_count = parse_pool_count(_em_get_json(
            "https://push2ex.eastmoney.com/getTopicDTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
            f"&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fund:asc&date={trade_date}"))
        if zt is not None or dt_count is not None:
            sentiment["limit"] = {"zt": zt, "dt": dt_count}

    top = parse_sector_list(_em_get_json(
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2"
        "&fid=f3&fs=m:90+t:2+f:!50&fields=f12,f14,f3"))
    bottom = parse_sector_list(_em_get_json(
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=0&np=1&fltt=2&invt=2"
        "&fid=f3&fs=m:90+t:2+f:!50&fields=f12,f14,f3"))
    if top or bottom:
        sentiment["sectors"] = {"top": top, "bottom": bottom}

    return sentiment


def format_sentiment_block(sentiment, trade_date_label=""):
    """把情绪硬指标格式化为 LLM 可读文本（缺失项自动省略）。"""
    if not sentiment:
        return ""
    suffix = f"（{trade_date_label}）" if trade_date_label else ""
    lines = [f"## 市场情绪硬指标{suffix}", ""]

    b = sentiment.get("breadth")
    if b:
        lines.append(f"- 涨跌家数（两市合计）：上涨 {b['up']} 家 / 下跌 {b['down']} 家 / 平盘 {b['flat']} 家")
        lines.append(f"- 两市成交额：{b['turnover_yi']:.0f} 亿元")
        if b.get("indexes"):
            lines.append("- 指数涨跌：" + "、".join(f"{n} {c:+.2f}%" for n, c in b["indexes"]))

    lim = sentiment.get("limit")
    if lim:
        parts = []
        if lim.get("zt") is not None:
            parts.append(f"涨停 {lim['zt']} 家")
        if lim.get("dt") is not None:
            parts.append(f"跌停 {lim['dt']} 家")
        if parts:
            lines.append("- 涨跌停：" + "、".join(parts))

    sec = sentiment.get("sectors")
    if sec:
        if sec.get("top"):
            lines.append("- 板块涨幅前五：" + "、".join(f"{n} {c:+.2f}%" for n, c in sec["top"]))
        if sec.get("bottom"):
            lines.append("- 板块跌幅前五：" + "、".join(f"{n} {c:+.2f}%" for n, c in sec["bottom"]))

    lines.append("")
    return "\n".join(lines)


# ============================================================
#  新闻抓取 & 格式化
# ============================================================

def fetch_all_news():
    """抓取全市场新闻 + 资金面数据（已通过 news_fetcher 聚合）。

    返回 (articles, fund_flow, errors)；errors 供运行摘要统计接口失败数。
    """
    articles, errors, fund_flow = fetch_all_news_flat("all")
    for err in errors:
        print(f"  [WARN] {err.get('error', str(err))}")
    return articles, fund_flow, errors


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


# 每市场送入 LLM 的条数上限 + 各来源的保底配额。
# 旧行为是 articles[:30]：源顺序为 快讯(20)+公告要闻(10)+公司新闻(10)+要闻栏目(10)+新浪滚动(10)，
# 前两个源就把 30 条吃满，导致后三个源（30 条原始请求量）每次都被整段截断丢弃。
NEWS_MARKET_CAP = 45
NEWS_SOURCE_QUOTAS = {
    "em102": 12,         # 东财·全部快讯（时效性最强）
    "em103": 8,          # 东财·公告要闻
    "em110": 8,          # 东财·公司新闻
    "em_news_list": 6,   # 东财·要闻栏目
    "sina_roll": 6,      # 新浪·滚动
    "em111": 10,         # 东财·美股快讯
    "em105": 8,          # 东财·全球要闻
    "hk_news": 12,       # 东财·港股栏目
}
NEWS_DEFAULT_QUOTA = 6


def select_news_by_quota(articles, per_market_cap=NEWS_MARKET_CAP, quotas=None,
                        default_quota=NEWS_DEFAULT_QUOTA):
    """按来源配额挑选新闻，保证多源覆盖（纯函数，便于回归测试）。

    先按来源各取保底 N 条，不足上限时再用剩余条目回填，最后受 per_market_cap 限制。
    """
    quotas = NEWS_SOURCE_QUOTAS if quotas is None else quotas
    buckets = {}
    for a in articles:
        key = a.get("bucket") or a.get("type") or ""
        buckets.setdefault(key, []).append(a)

    picked, overflow = [], []
    for key, items in buckets.items():
        quota = quotas.get(key, default_quota)
        picked.extend(items[:quota])
        overflow.extend(items[quota:])

    if len(picked) < per_market_cap:
        picked.extend(overflow[:per_market_cap - len(picked)])
    return picked[:per_market_cap]


def format_news(news_list, fund_flow=None, sentiment=None, trade_date_label=""):
    """将多市场新闻、资金面和情绪硬指标格式化为 LLM 可读文本。

    sentiment: fetch_market_sentiment() 的结果，无数据时传 None/{}（该段自动省略）。
    """
    today_str = beijing_now().strftime("%Y-%m-%d")
    day_desc = "是交易日" if beijing_now().weekday() < 5 else "非交易日（周末）"

    lines = [
        f"日期: {beijing_now().strftime('%Y-%m-%d %H:%M')}",
        f"今日 {today_str} {day_desc}，以下为当日多市场资讯汇总。",
        "",
    ]

    # ---- 情绪硬指标（最优先参考，缺失自动省略）----
    sent_block = format_sentiment_block(sentiment or {}, trade_date_label)
    if sent_block:
        lines.append(sent_block)

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
        selected = select_news_by_quota(articles)
        lines.append(f"\n## {mkt_name} ({len(articles)}条)")
        if len(articles) < 5:
            lines.append(f"（样本不足：该市场仅 {len(articles)} 条资讯，相关判断请谨慎，不要下重结论）")
        elif len(selected) < len(articles):
            lines.append(f"（按来源配额精选 {len(selected)} 条送入分析）")

        for i, a in enumerate(selected, 1):
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
    """从 prompts/ 目录加载提示词模板（锚定脚本目录，不依赖 CWD）。"""
    prompt_path = os.path.join(_BASE_DIR, "prompts", name)
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

def _strip_thinking(text):
    """兜底：剔除 LLM 把思考过程/任务复述写进正文的情况。

    即使提示词已禁止，推理模型偶尔仍会输出内心独白。这里做两道机械裁剪：
      1) 若正文起点（首个 ### / ## / - / | 内容行）之前存在元评论，整段丢弃前缀；
      2) 删除行首即以典型元评论短语开头的整行。
    本函数保持通用（不对特定标记做硬锚定），选股专用的「首个 ### 锚定」
    由 _sanitize_stock_picks 负责，避免误伤以 ## 开头的盘面分析。
    """
    import re as _re
    # 防御：下游若误传非字符串（如未解包的元组），直接原样返回，避免整条流水线崩溃
    if not isinstance(text, str):
        print(f"[WARN] _strip_thinking 收到非字符串输入（{type(text).__name__}），已跳过清洗")
        return text
    if not text or not text.strip():
        return text
    lines = text.split("\n")

    # ---- 第 1 道：截掉开头的思考前缀 ----
    content_markers = ("###", "☆", "## ", "##\t", "- ", "* ", "|")
    start_idx = None
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if any(s.startswith(m) for m in content_markers):
            start_idx = i
            break
    if start_idx is not None and start_idx > 0:
        preamble = "\n".join(lines[:start_idx])
        if _re.search(
            r"好的[，。：]|让我(仔细|先|梳理|想想)|等一下|用户(要求|给|希望)|我需要|接下来|先看一下|"
            r"首先我|梳理(一下|任务)|其实对于|按照用户|我理解为|我的理解是|这是(一道|一个任务)|"
            r"这个要小心|再仔细考虑|我得知道|若我不确定|评级怎么分配|下面我来|现在分析|我们来分析|"
            r"我思考|我的思路|康美特|应该怎么处理|其实这里|"
            r"就说|用[「\"]|需要确认的是|不纠结|不提|我倾向(给|给)?|如果落实到(投资)?|"
            r"为了表格|不过用户|用户严格要求|注意是|再看资金面|具体各大行的|温度判断|"
            r"今日要点\d*行|主线扫描\d|资金面$|跨市场[：:]美股",
            preamble,
        ):
            text = "\n".join(lines[start_idx:])
            lines = text.split("\n")

    # ---- 第 2 道：删掉残留的整行元评论 ----
    meta_openers = (
        "好的，", "好的。", "好的：", "让我仔细", "让我先", "让我梳理", "让我想想",
        "等一下，", "等一下：", "用户要求", "用户给", "用户希望",
        "我需要", "我应先", "接下来，", "接下来我", "先看一下", "先看看",
        "首先我", "首先，让我", "梳理一下", "梳理任务", "其实对于", "其实这里",
        "按照用户", "我理解为", "我的理解是", "我的理解", "这是一道", "这是一个任务",
        "这个要小心", "再仔细考虑", "我得知道", "若我不确定", "评级怎么分配",
        "下面我来", "现在分析", "我们来分析", "我思考", "我的思路",
        "就说", "用「", "用\"", "需要确认的是", "不纠结", "不提", "我倾向给",
        "我倾向", "如果落实到投资", "如果落实到", "为了表格", "不过用户",
        "用户严格要求", "注意是", "再看资金面", "具体各大行的", "温度判断",
    )
    cleaned = []
    for ln in lines:
        s = ln.lstrip()
        if s.startswith(meta_openers) and not s.startswith(("###", "##", "|", "- ", "* ")):
            continue
        cleaned.append(ln)
    return "\n".join(cleaned)


# 选股生成异常时的降级文案（绝不把草稿推上 Pages）
PICKS_FALLBACK = (
    "今日 AI 选股生成异常（模型未返回结构化结果），本节已跳过，"
    "未发布任何未经验证的草稿。请检查提示词或重新运行工作流。"
)

ANALYST_FALLBACK = (
    "今日盘面分析生成异常（模型输出被长度限制截断，内容不完整），本节已跳过，"
    "未发布任何未完成的草稿。请提高 max_tokens 或重新运行工作流。"
)


def _sanitize_stock_picks(text):
    """选股专用清洗：以首个 ### 为唯一合法起点，剥离思考草稿；失败返回 None。

    选股提示词要求正文「只能以第一个 ### 小节开头」且禁止思考过程，但推理模型
    仍可能把整段独白写进正文。这里做选股特有的强裁剪：
      1) 截取首个 ### 标题及其之后内容，其前全部丢弃；
      2) 逐行删除自问式独白（以 ？/? 结尾且含 怎么/如何/应该/是否/为什么 等）；
      3) 逐行删除元评论短语开头的行；
      4) 删除独立的 ☆ 分隔行（与收藏按钮 glyph 冲突，且违学术风）；
      5) 若结果不含任何 ### 标题或表格/列表，判定生成失败，返回 None。
    """
    import re as _re
    if not text or not text.strip():
        return None

    # 0) 提示词回声检测：模型把提示词里的格式说明 / 模板占位符原样输出时，
    #    整段都是废稿（2026-09-07 线上事故：输出了「| 股票以及代码 |…|」「（表格至少3-5行）」
    #    「☆标题」「需要区分：」「先梳理资讯：」等），必须整体判失败，不能靠逐行删。
    # 注意：表头「| 股票及代码 | 选股逻辑 | 方向 | 评级 |」是模型照抄示例的正常行为，
    # 不能算回声；真正的回声是提示词里的「过程性指令」被原样输出。
    echo_patterns = (
        "（表格至少", "(表格至少", "表格至少3",
        "每条新闻只用一次", "每条资讯只用一次",
        "需要区分：", "需要区分:", "需要禁止", "需要确认的是", "需要检查", "需要建立",
        "先梳理资讯", "先梳理资讯：", "需要梳理资讯",
        "可能的主题", "完整输出结构",
        "用户要求", "用户说", "用户强调", "注意用户", "用户禁止",
    )
    if any(p in text for p in echo_patterns):
        print("  ⚠️ AI选股检测到提示词回声（模板/指令原文被输出），判定生成失败")
        return None

    lines = text.split("\n")

    # 1) 首个 ### 为起点
    start = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("###"):
            start = i
            break
    if start is None:
        return None  # 完全没有结构化标题 → 生成失败
    lines = lines[start:]

    meta_openers = (
        "好的，", "好的。", "让我", "等一下", "用户", "我需要", "接下来", "先看一下",
        "首先我", "梳理", "其实对于", "其实这里", "按照用户", "我理解为", "我的理解",
        "这是一道", "这是一个任务", "这个要小心", "再仔细考虑", "我得知道", "若我不确定",
        "评级怎么分配", "下面我来", "现在分析", "我们来分析", "我思考", "我的思路",
    )
    self_q = _re.compile(r"[？?]")  # 句中或句尾含问号均视为自问
    self_q_kw = ("怎么", "如何", "应该", "是否", "为什么", "能不能", "要不要", "该不该", "分配")

    out = []
    for ln in lines:
        s = ln.lstrip()
        # 合法结构行直接保留
        if s.startswith(("###", "##", "|", "- ", "* ", ">")):
            out.append(ln)
            continue
        # 独立 ☆ 分隔行删除
        if _re.fullmatch(r"☆+", s):
            continue
        # 元评论行删除
        if s.startswith(meta_openers):
            continue
        # 自问式独白删除
        if self_q.search(s) and any(k in s for k in self_q_kw):
            continue
        out.append(ln)

    result = "\n".join(out).strip()

    # 5) 结构化强校验
    #    5a) 必须存在真实 ### 标题（排除「标题」「主题名」「XXX」等未替换的占位符）
    placeholder_titles = {"标题", "主题", "主题名", "主题名称", "xxx", "XXX", "标题名", "小节"}
    real_titles = []
    for m in _re.finditer(r"^\s*#{2,3}\s*(.+?)\s*$", result, _re.MULTILINE):
        t = _re.sub(r"[☆★*`\s]+", "", m.group(1))
        if t and t not in placeholder_titles:
            real_titles.append(t)
    if not real_titles:
        return None

    #    5b) 必须存在「完整表格」：表头 + 分隔行 |---| + 至少 3 行数据行。
    #        仅有一行 `| 股票及代码 |…|` 属模板回显，不算表格。
    has_full_table = False
    lines_r = result.split("\n")
    for i, ln in enumerate(lines_r):
        if not _re.match(r"^\s*\|[\s\-:|]+\|\s*$", ln):
            continue
        if i == 0 or not _re.match(r"^\s*\|.*\|\s*$", lines_r[i - 1]):
            continue  # 没有表头行
        data_rows = 0
        for j in range(i + 1, len(lines_r)):
            if _re.match(r"^\s*\|.*\|\s*$", lines_r[j]):
                data_rows += 1
            else:
                break
            if data_rows >= 3:
                has_full_table = True
                break
        if has_full_table:
            break

    if not has_full_table:
        return None
    return result


def _sanitize_analyst(text):
    """盘面分析专用清洗：锚定首个真实内容行，剥离思考草稿与结构清单回声。

    盘面提示词要求「直接以第一个 `- **市场体温**：` 要点开头」，但推理模型仍可能：
      1) 把提示词里的结构清单（今日要点5行 / 资金面 / 主线扫描…）原样复述到开头；
      2) 在正文里夹带自问式独白（如「就说偏暖吧」「用「偏暖」」「不纠结」）。
    这里做盘面特有的强裁剪（与 _sanitize_stock_picks 不同，本函数不返回 None，
    盘面无降级文案机制，仅尽力清洗；硬约束主要由提示词保证）：
      1) 以首个真实内容行（`- **` 要点 / `## ` 标题 / 数字编号 / 章节名）为起点，其前全丢；
      2) 逐行删除自问式独白（含 ？/? 且带 怎么/如何/应该/是否/用哪个 等）；
      3) 逐行删除元评论短语开头的行；
      4) 删除独立的 ☆ 分隔行。
    带 `- **` 标签的内容要点一律保留，避免误伤正常输出。
    """
    import re as _re
    if not text or not text.strip():
        return text
    lines = text.split("\n")

    # 1) 锚定首个真实内容行。
    #    只认「带标签要点 - **」与「标题 ##/###」两种。
    #    绝不认裸 `- ` / `* ` 列表与裸章节名：实测模型会把结构清单写成
    #    「- 首段必须是今日要点5行…」「- 资金面 1-2句」这类 bullet，
    #    若把 `- ` 当起点则 start=0，前置思考一行都剪不掉（9/7 线上事故根因）。
    start = None
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith(("- **", "**", "##")):
            start = i
            break
    if start is not None and start > 0:
        lines = lines[start:]

    # 2) 逐行剥离元评论 / 自问独白（带标签要点与标题先放行）
    meta_openers = (
        "好的，", "好的。", "好的：", "让我", "等一下", "用户", "我需要", "接下来", "先看一下",
        "首先我", "梳理", "其实对于", "其实这里", "按照用户", "我理解为", "我的理解",
        "这是一道", "这是一个任务", "这个要小心", "再仔细考虑", "我得知道", "若我不确定",
        "评级怎么分配", "下面我来", "现在分析", "我们来分析", "我思考", "我的思路",
        "就说", "用「", "用\"", "需要确认的是", "不纠结", "不提", "我倾向给", "我倾向",
        "如果落实到投资", "如果落实到", "为了表格", "不过用户", "用户严格要求", "注意是",
        "再看资金面", "具体各大行的", "温度判断", "最大共识：", "最大分歧：", "最大风险：",
        "一句话策略：", "今日要点第一行", "整体放摘要", "具体写作", "也可以选",
        "先看资讯", "新闻里其他", "盘面温度判断", "可能写不下", "另外要注意",
        "等等，", "等等:", "措辞要决断", "注意这里的环境", "只能说",
    )
    self_q = _re.compile(r"[？?]")
    self_q_kw = ("用哪个", "加不加息", "算什么", "按什么", "该不该", "要不要")

    out = []
    for ln in lines:
        s = ln.lstrip()
        # 结构标题 / 表格 / 引用 / 带标签要点：直接保留
        if s.startswith(("###", "##", "|", "* ", "**", ">")) or s.startswith("- **"):
            out.append(ln)
            continue
        # 独立 ☆ 分隔行删除
        if _re.fullmatch(r"☆+", s):
            continue
        # 元评论行删除
        if s.startswith(meta_openers):
            continue
        # 自问式独白删除
        if self_q.search(s) and any(k in s for k in self_q_kw):
            continue
        out.append(ln)

    return "\n".join(out).strip()


def _cleanup_report(text, strip_bold=False):
    """清洗 LLM 生成的报告：移除 #### / *** 标记，并兜底剔除思考过程。

    Args:
        text: 原始文本
        strip_bold: 是否移除 ** 加粗标记（选股输出用，主报告保留）
    """
    import re as _re

    # 0. 兜底：剔除可能泄漏的思考过程 / 任务复述
    text = _strip_thinking(text)

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

def _post_deepseek(payload, headers, url):
    """发起一次 DeepSeek 请求，返回 (content, finish_reason)。"""
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw_body = resp.read().decode()
    data = json.loads(raw_body)
    msg = data["choices"][0]["message"]
    content = msg.get("content", "")
    finish = data["choices"][0].get("finish_reason", "?")
    # 推理型模型可能把正文放在 reasoning_content，content 为空时回退
    if not content or not content.strip():
        content = msg.get("reasoning_content", "")
    # 仍为空则打印原始响应，便于定位（限流 / 内容过滤 / 字段变更等）
    if not content or not content.strip():
        print(f"[LLM-DEBUG] 空返回 finish_reason={finish} 原始响应前 600 字:\n{raw_body[:600]}")
        return ("API 调用失败: 返回内容为空", finish)
    return (content, finish)


def _call_deepseek(system_prompt, user_prompt, temperature=0.5, max_tokens=4096):
    """通用 DeepSeek API 调用，返回 (content, finish_reason)。

    关键：deepseek-v4-flash 会把推理链写进 content（而非 reasoning_content），
    思考会吃掉大量 token 导致正文被截断。这里显式传 thinking=disabled 请求关闭思考；
    若服务端不识别该参数（返回 400），自动去掉重试，保证兼容性。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return ("错误：未设置 DEEPSEEK_API_KEY 环境变量", "error")

    url = "https://api.deepseek.com/v1/chat/completions"
    base = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    # 先尝试关闭思考（治本：不产出推理链，就不会被写进正文）
    try:
        payload = dict(base, thinking={"type": "disabled"})
        return _post_deepseek(json.dumps(payload).encode("utf-8"), headers, url)
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        if "thinking" not in body.lower():
            return (f"API 调用失败: HTTP {e.code} - {body[:300]}", "error")
        # 服务端不识别 thinking 参数 → 去掉重试
        print("[LLM] 服务端不识别 thinking 参数，已自动回退为普通请求")
    except Exception as e:
        return (f"API 调用失败: {str(e)}", "error")

    try:
        return _post_deepseek(json.dumps(base).encode("utf-8"), headers, url)
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return (f"API 调用失败: HTTP {e.code} - {body[:300]}", "error")
    except Exception as e:
        return (f"API 调用失败: {str(e)}", "error")


def _call_deepseek_safe(system_prompt, user_prompt, temperature=0.5, max_tokens=4096, section_name="AI分析"):
    """带优雅降级的 DeepSeek API 调用。失败时返回友好提示而非原始错误文本。

    返回 (text, truncated)：
      - text：正文（失败时为降级文案）
      - truncated：是否因 finish_reason=length 被截断。截断的内容 100% 不完整
        （表现为正文写一半或仍在思考阶段中断），调用方应据此降级，不可直接发布。
    """
    result, finish = _call_deepseek(system_prompt, user_prompt, temperature, max_tokens)
    # 空返回多为并行调用下的瞬时限流，短暂等待后重试一次
    if result == "API 调用失败: 返回内容为空":
        print(f"[LLM] {section_name} 返回空内容，3 秒后重试一次...")
        time.sleep(3)
        result, finish = _call_deepseek(system_prompt, user_prompt, temperature, max_tokens)
    # 429/5xx 为限流或服务端瞬时故障，退避后重试一次（此前直接降级，白白丢一节内容）
    if result.startswith("API 调用失败") and any(
        c in result for c in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503")
    ):
        print(f"[LLM] {section_name} 遇到限流/服务端错误，15 秒退避后重试一次...")
        time.sleep(15)
        result, finish = _call_deepseek(system_prompt, user_prompt, temperature, max_tokens)
    if result.startswith("错误") or result.startswith("API 调用失败"):
        print(f"[LLM] {section_name} 调用失败，使用降级: {result[:100]}")
        return (f"*({section_name}暂时不可用，请稍后重试)*", False)

    if finish == "length":
        print(f"[LLM] ⚠️ {section_name} 输出被 max_tokens 截断（finish_reason=length），内容不完整")
        return (result, True)
    return (result, False)


def call_llm(news_text):
    """调用 LLM 生成市场分析报告，并清理 #### / *** 标记与思考过程。

    返回 (正文, 状态)，状态 ∈ {"ok", "fallback"}，供运行摘要展示。
    """
    # 注意：deepseek-v4-flash 会把推理链写进 content（而非 reasoning_content），
    # 思考约耗 4000-5000 token。若额度只给正文，正文会被截断（9/7 线上停在"理由："）。
    # 故给足额度，让"思考+正文"都写得完，再由 _sanitize_analyst 裁掉思考部分。
    raw, truncated = _call_deepseek_safe(SYSTEM_PROMPT, USER_PROMPT_TEMPLATE.format(news_text=news_text),
                                         temperature=0.5, max_tokens=12000, section_name="市场分析")
    text = _sanitize_analyst(_cleanup_report(raw))
    # 截断完整性判断：不依赖单一魔法字符串，校验提示词要求的 6 个固定小节是否齐全
    required_sections = ("## 资金面", "## 主线扫描", "## 跨市场", "## 要闻速览", "## 市场体温", "## 明天怎么看")
    if truncated and not all(s in text for s in required_sections):
        # 被截断且结构不完整 → 内容残缺，宁缺毋滥
        print("  ⚠️ 盘面分析被截断且结构不完整，返回降级文案")
        return (ANALYST_FALLBACK, "fallback")
    return (text, "ok")


def call_stock_picker(news_text, opinion_context="", info_context=""):
    """调用 LLM 执行产业链选股分析（融合新闻+UP主观点+信息差），并清洗输出（含去 **）。

    经 _cleanup_report 通用清洗后，再用选股专用的 _sanitize_stock_picks 做
    「首个 ### 锚定 + 自问句剥离 + 结构化校验」；若校验失败（模型吐出思考草稿
    或无表格），返回 PICKS_FALLBACK 降级文案，绝不把草稿推上 Pages。
    """
    raw, truncated = _call_deepseek_safe(STOCK_PICKER_SYSTEM_PROMPT,
                              STOCK_PICKER_TEMPLATE.format(
                                  news_text=news_text,
                                  opinion_context=opinion_context,
                                  info_context=info_context,
                              ),
                              temperature=0.3, max_tokens=8000, section_name="AI选股")
    if truncated:
        # 被 max_tokens 截断 → 表格/正文 100% 不完整，残缺草稿风险高，宁缺毋滥
        print("  ⚠️ AI选股输出被截断（max_tokens），内容不完整，返回降级文案，不发布草稿")
        return PICKS_FALLBACK
    cleaned = _cleanup_report(raw, strip_bold=True)
    sanitized = _sanitize_stock_picks(cleaned)
    if sanitized is None:
        print("  ⚠️ AI选股生成异常（思考泄漏或无结构化输出），返回降级文案，不发布草稿")
        return PICKS_FALLBACK
    return sanitized


def format_stock_picks(picks_md):
    """将选股结果封装为日报板块（前置方法论 + 免责声明）。"""
    if not picks_md or not picks_md.strip():
        return ""
    disclaimer = (
        "\n\n---\n\n"
        "> 以下分析由 AI 基于当日新闻资讯、UP主观点共识与产业链逻辑推导生成，仅供研究参考，不构成投资建议。\n\n"
        "## AI选股\n\n"
    )
    return disclaimer + f"{picks_md}\n"


def call_opinion_analyzer(opinion_text):
    """调用 LLM 分析UP主财经观点，返回结构化 markdown。

    截断的蒸馏结果是半成品，不能发布；返回空串触发主流程「展示原文」降级，
    原文是完整输入，内容零丢失。
    """
    raw, truncated = _call_deepseek_safe(
        OPINION_SYSTEM_PROMPT,
        OPINION_USER_PROMPT_TEMPLATE.format(opinion_text=opinion_text),
        temperature=0.4,
        max_tokens=4096,
        section_name="UP主观点蒸馏",
    )
    if truncated:
        print("  ⚠️ UP主观点蒸馏被截断（内容不完整），降级为展示原文")
        return ""
    return _cleanup_report(raw)


def call_info_analyzer(info_text):
    """调用 LLM 提炼信息差，提取核心事实（不做多空判断）。

    截断的提炼结果不发布；返回空串触发主流程「信息差补充（原文）」降级。
    """
    raw, truncated = _call_deepseek_safe(
        INFO_GAP_SYSTEM_PROMPT,
        INFO_GAP_USER_PROMPT_TEMPLATE.format(info_text=info_text),
        temperature=0.3,
        max_tokens=3072,
        section_name="信息差提炼",
    )
    if truncated:
        print("  ⚠️ 信息差提炼被截断（内容不完整），降级为展示原文")
        return ""
    return _cleanup_report(raw)



# ============================================================
#  Markdown → HTML（Bloomberg Terminal 风格）
# ============================================================

def _esc_html(text):
    """最小 HTML 转义，阻断第三方新闻标题 / LLM 输出中的标签注入。

    注入链路：数据源 title/summary → LLM 回流 → markdown_to_html → innerHTML。
    不转义 &，避免对数据源已有的 HTML 实体（如 &amp;）二次转义。
    """
    if not isinstance(text, str):
        return text
    return text.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _inline_md(text):
    """行内 markdown：加粗 + 关键判断词高亮（先转义再包标签）。"""
    text = _esc_html(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return _highlight_inline(text)


def _is_opinion_meta(text):
    """判断列表项是否为 UP主结构化字段（情绪/风格/一句话操作），供意见模式下去重。"""
    return bool(re.search(r"(情绪倾向|风格|一句话操作)(?:</?strong>)?\s*[：:]", text))


def _render_opinion_tags(raw_lines):
    """从 UP主小节的原始行中提取情绪/风格/一句话操作，渲染为标签条。"""
    # raw 行可能已被加粗为 <strong>，先去除内联标签再按原始 markdown 匹配
    text = re.sub(r"<[^>]+>", "", "\n".join(raw_lines))
    sent = re.search(r"情绪倾向[：:]\s*(利好|中性|利空)", text)
    style = re.search(r"风格[：:]\s*(.+)", text)
    action = re.search(r"一句话操作[：:]\s*(.+)", text)
    chips = []
    if sent:
        v = sent.group(1)
        cls = {"利好": "op-bull", "中性": "op-neu", "利空": "op-bear"}[v]
        chips.append(f'<span class="op-chip {cls}">{v}</span>')
    if style:
        chips.append(f'<span class="op-chip op-style">{_esc_html(style.group(1).strip())}</span>')
    if not chips and not action:
        return ""
    html = '<div class="op-tags">' + "".join(chips) + '</div>'
    if action:
        html += f'<div class="op-action"><span class="op-action-k">一句话操作</span>{_inline_md(action.group(1).strip())}</div>'
    return html


def markdown_to_html(md, mode="default"):
    """两阶段 Markdown→HTML 转换，支持表格和图片。
    任意含「情绪倾向」的 ### 小节会自动渲染情绪/风格标签与一句话操作（UP主观点）。
    """

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
                header_cells = [_esc_html(c.strip()) for c in stripped.split("|")[1:-1]]
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
                    cells = [_esc_html(c.strip()) for c in row_line.split("|")[1:-1]]
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
    current_section = {"title": "", "content": [], "raw": []}

    def _push(cur):
        if cur["title"] or cur["content"]:
            sections.append(cur)

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
            _push(current_section)
            current_section = {"title": h_match.group(1), "content": [], "raw": []}
            continue

        # 图片
        img_match = re.match(r"^!\[(.+)\]\((.+)\)$", line)
        if img_match:
            alt = img_match.group(1)
            url = img_match.group(2)
            alt = _esc_html(alt)
            url = _esc_html(url)
            alt = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", alt)
            current_section["content"].append(("img", alt, url))
            continue

        # 引用（免责声明 / 说明）
        if line.startswith(">"):
            current_section["content"].append(("note", _inline_md(line[1:].strip())))
            current_section["raw"].append(line)
            continue

        # 行内加粗（先转义外部文本再包 <strong>，阻断标签注入）
        line = _esc_html(line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)

        # 有序列表
        ol_match = re.match(r"^(\d+)\.\s+(.+)$", line)
        if ol_match:
            current_section["content"].append(("ol", ol_match.group(1), _highlight_inline(ol_match.group(2))))
            current_section["raw"].append(line)
            continue

        # 无序列表
        ul_match = re.match(r"^[-*]\s+(.+)$", line)
        if ul_match:
            current_section["content"].append(("ul", _highlight_inline(ul_match.group(1))))
            current_section["raw"].append(line)
            continue

        if line == "---":
            continue

        current_section["content"].append(("p", _highlight_inline(line)))
        current_section["raw"].append(line)

    _push(current_section)

    # 渲染
    import hashlib as _hl
    html_parts = []
    for sec_idx, sec in enumerate(sections):
        sec_title = sec.get("title", "")
        # 生成唯一 section ID（基于标题 hash）
        sec_id = _hl.md5(sec_title.encode()).hexdigest()[:10] if sec_title else f"s{sec_idx}"
        html_parts.append(f'<div class="sec" id="{sec_id}" data-section-id="{sec_id}">')
        if sec["title"]:
            # 去序号前缀；并剥掉模型惯用的 ☆ 装饰（与收藏按钮 glyph 冲突，且违学术风）
            clean_title = re.sub(r"^[一二三四五六七八九十]+[、．.]?\s*", "", sec["title"])
            clean_title = re.sub(r"^[☆★]+\s*", "", clean_title).strip()
            clean_title = _esc_html(clean_title)
            html_parts.append(
                f'<h3 class="sec-h">'
                f'<button class="fav-btn" data-sid="{sec_id}" '
                f'title="收藏此条分析" onclick="toggleFav(this)">☆</button>'
                f'{clean_title}</h3>'
            )
        # UP主小节（非共识/分歧）渲染情绪/风格标签 + 一句话操作
        if sec_title and "共识" not in sec_title and "分歧" not in sec_title:
            tag_html = _render_opinion_tags(sec.get("raw", []))
            if tag_html:
                html_parts.append(tag_html)
        for item in sec["content"]:
            typ = item[0]
            if typ == "ol":
                html_parts.append(
                    f'<div class="ni"><span class="ni-num">{item[1]}</span>'
                    f'<div class="ni-text">{item[2]}</div></div>'
                )
            elif typ == "ul":
                if _is_opinion_meta(item[1]):
                    continue
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
            elif typ == "note":
                html_parts.append(f'<p class="note">{item[1]}</p>')
            else:
                html_parts.append(f'<p class="para">{item[1]}</p>')
        html_parts.append("</div>")

    return "\n".join(html_parts)


# ============================================================
#  HTML 报告生成 —  Bloomberg Terminal Dark 审美
# ============================================================

_LEAD_KEYS = ("市场体温", "最大共识", "最大分歧", "最大风险", "一句话策略")


def _split_lead(md, title="今日要点"):
    """从 markdown 中抽取「今日要点」区块，返回 (lead_md, rest_md)。

    两种写法都支持：
      1) 有 `## 今日要点` 标题 → 抽取该标题下到下一个 `## ` 前的内容；
      2) 无标题、开头直接是 `- **市场体温**：…` 五行要点（模型常见写法）→
         直接从开头收集连续的要点行。此前只支持第 1 种，导致线上 lead-box 长期为空。
    """
    lines = md.split("\n")
    start = None
    pat = re.compile(r"^##\s+" + re.escape(title) + r"\s*$")
    for i, ln in enumerate(lines):
        if pat.match(ln.strip()):
            start = i
            break

    if start is None:
        # fallback：开头连续的「- **要点键**：值」行即为今日要点
        collected, idx = [], 0
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not s:
                if collected:
                    idx = i
                    break
                continue
            m = re.match(r"^[-*]\s+\*\*(.+?)\*\*\s*[：:]\s*(.+)$", s)
            if not m:
                break
            collected.append(s)
            idx = i + 1
        # 至少含 3 个今日要点固定键，才认定是要点区块（避免误抽普通列表）
        hit = sum(1 for c in collected if any(k in c for k in _LEAD_KEYS))
        if len(collected) >= 3 and hit >= 3:
            rest = lines[idx:]
            return "\n".join(collected), "\n".join(rest).strip()
        return "", md
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^##\s+", lines[j].strip()):
            end = j
            break
    lead = "\n".join(lines[start + 1:end]).strip()
    rest = "\n".join(lines[:start] + lines[end:]).strip()
    return lead, rest


def _render_lead_box(lead_md):
    """将 '## 今日要点' 区块渲染为学术风置顶摘要框。"""
    rows = []
    for ln in lead_md.split("\n"):
        m = re.match(r"^[-*]\s+\*\*(.+?)\*\*[：:]\s*(.+)$", ln.strip())
        if m:
            rows.append(
                f'<div class="lead-row">'
                f'<span class="lead-k">{_esc_html(m.group(1))}</span>'
                f'<span class="lead-v">{_highlight_inline(_esc_html(m.group(2).strip()))}</span>'
                f'</div>'
            )
    if not rows:
        return ""
    return (
        '<div class="lead-box">'
        '<div class="lead-h">今日要点 · DAILY BRIEF</div>'
        '<div class="lead-body">' + "".join(rows) + '</div>'
        '</div>'
    )


def _load_asset(rel_path):
    """读取 docs/ 下的静态资源（style.css / script.js），用于内联进报告。

    读取失败返回 None，调用方会回退为外链，保证生成不中断。
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel_path)
    try:
        with open(p, encoding="utf-8") as _f:
            return _f.read()
    except OSError:
        return None


def generate_html_report(report, quotes, news_list, page_url="", page_base_url="",
                         fund_flow=None, session_label="早报", session_slug="am",
                         opinion_html="", opinion_title="今日收盘UP主观点",
                         style_css=None, script_js=None, up_count=0, report_time=None):
    """生成 Bloomberg Terminal 风格 HTML 详情页。

    report_time 为报告【归属日期】（延迟运行时可能是昨天），未传则取当前时间。
    """
    bj_now = report_time or beijing_now()
    today = bj_now.strftime("%Y-%m-%d")
    today_en = bj_now.strftime("%B %d, %Y")
    now_str = bj_now.strftime("%H:%M")
    update_datetime = beijing_now().strftime("%Y-%m-%d %H:%M")  # 实际生成时间戳，页面展示用
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday = weekday_names[bj_now.weekday()]
    wk_cn = weekday_cn[bj_now.weekday()]
    fav_date_key = f"{today}_{session_slug}"  # 用于收藏唯一标识

    # ---- 历史简报导航 ----
    history_links_html = ""
    if page_base_url:
        docs_dir = os.path.join(_BASE_DIR, "docs")
        for i in range(1, 6):
            d = bj_now - timedelta(days=i)
            dslug = d.strftime("%Y%m%d")
            # 只渲染本地已存在的归档，避免导航死链
            if not os.path.exists(os.path.join(docs_dir, f"report_{dslug}.html")):
                continue
            label = d.strftime("%m月%d日")
            history_links_html += (
                f'<a class="hl" href="{page_base_url}report_{dslug}.html">'
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
        total_color = "#9c3b3b" if total_flow >= 0 else "#3f6b4f"
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
            color = "#9c3b3b" if is_pos else "#3f6b4f"
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

    # ---- 报告正文（抽取「今日要点」为置顶摘要）----
    lead_md, report_rest = _split_lead(report, "今日要点")
    lead_box_html = _render_lead_box(lead_md) if lead_md else ""
    report_html = markdown_to_html(report_rest)

    # ---- 目录锚点导航 ----
    import hashlib as _hl
    toc_items = [
        ("盘面分析与研判", "sec-analysis"),
        ("主力资金流向", "sec-fund"),
        ("分时走势", "sec-intraday"),
    ]
    for _m in re.finditer(r"^##\s+(.+)$", report_rest, re.M):
        _t = _m.group(1).strip()
        if _t == "今日要点":
            continue
        toc_items.append((_t, _hl.md5(_t.encode()).hexdigest()[:10]))
    toc_html = (
        '<nav class="toc"><span class="toc-label">快速跳转</span>'
        + "".join(f'<a class="toc-link" href="#{_tid}">{_t}</a>' for _t, _tid in toc_items)
        + '</nav>'
    )
    up_src = f" · 综合 {up_count} 位 UP 主观点" if up_count > 0 else ""

    # ---- 观点蒸馏板块 ----
    if opinion_html:
        opinion_section = (
            f'  <div class="sec-hdr" id="sec-opinion" style="margin-top:40px;"><span class="sec-main">{opinion_title}</span><span class="sec-sub">OPINION DISTILLATION · 自媒体观点蒸馏</span></div>\n'
            f'  <div class="report-body opinions-body">{opinion_html}</div>\n'
        )
    else:
        opinion_section = ""

    # ---- 内联静态资源（旧模板格式：自包含，不再外链 style.css / script.js）----
    if style_css is None:
        style_css = _load_asset("docs/style.css") or ""
    if script_js is None:
        script_js = _load_asset("docs/script.js") or ""
    css_link = (f'<style>\n{style_css}\n</style>' if style_css
                else f'<link rel="stylesheet" href="{page_base_url}style.css?v={today}">')
    js_block = (f'<script>\n{script_js}\n</script>' if script_js
                else f'<script src="{page_base_url}script.js?v={today}" defer></script>')

    # ---- 组装 ----
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MARKET BRIEF · {today} {session_label}</title>
{css_link}
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
  <div class="masthead-src">数据来源：东方财富 · 新浪财经{up_src}</div>
  <div class="masthead-by">作者＆模型：HZT ＆ Deepseek V4 Flash</div>
</div>

{toc_html}

<div class="content">

  {lead_box_html}

  <!-- FUND FLOW PANEL -->
  <div class="sec-hdr" id="sec-fund"><span class="sec-main">主力资金流向</span><span class="sec-sub">CAPITAL FLOWS · 沪深300口径 · 近5交易日 · 东方财富</span></div>
  <div class="fp-grid">{fund_panel}</div>

  <!-- INTRADAY CHARTS -->
  <div class="sec-hdr" id="sec-intraday"><span class="sec-main">分时走势</span><span class="sec-sub">INTRADAY · 上证指数 / 深证成指 · 新浪财经</span></div>
  <div class="charts-row">{chart_html}</div>

  <!-- AI ANALYSIS -->
  <div class="sec-hdr" id="sec-analysis"><span class="sec-main">盘面分析与研判</span><span class="sec-sub">ANALYSIS · 由 AI 生成 · 仅供研究参考</span></div>
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

<!-- ══════════════ 回到顶部 ══════════════ -->
<button class="to-top" id="toTop" onclick="window.scrollTo({{top:0,behavior:'smooth'}})" title="回到顶部">↑</button>

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
{js_block}

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
      - charts/:       删除文件名中日期超过 `days` 天的 PNG 文件
      - pdf/:          删除文件名中日期超过 `days` 天的 PDF（保留 latest.pdf）
      - 归档 HTML：     永不删除（用户要求保留全部历史简报与 UP 主内容）
      - max_per_run: 单次最多删除数量，防止首次运行产生超大 commit
    """
    import glob as _glob
    import re as _re

    today = beijing_now()
    cutoff_date = (today - timedelta(days=days)).date()
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
        dir_path = os.path.join(_BASE_DIR, "docs", subdir)
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
        print(f"[Cleanup] 已清理 {total_removed} 个旧文件"
              + (f", 剩余 {skipped} 个将在后续运行中逐步清理" if skipped else ""))
    else:
        print("[Cleanup] 无需清理")


def generate_pdf(html_path):
    """将 HTML 报告转为 PDF（Chrome Headless）。"""
    import subprocess
    import shutil

    now = beijing_now()
    pdf_filename = f"股市简报_{now.strftime('%Y-%m-%d_%H%M')}.pdf"
    pdf_dir = os.path.join(_BASE_DIR, "docs", "pdf")
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
    """将 HTML 写入 docs/ 目录：场次文件（真实内容）+ index.html + 主文件跳转桩。

    历史做法把同一份 HTML 写三遍（report_日期.html / report_日期_场次.html /
    index.html），每天白白多存 1-2 份 40-70KB 的完全相同副本。
    现改为：真实内容只写场次文件与 index.html；无后缀主文件降级为 <1KB 的
    跳转桩，老链接（含历史导航、收藏夹跨报告跳转）仍然可用。
    """
    today = beijing_now().strftime("%Y%m%d")
    docs_dir = os.path.join(_BASE_DIR, "docs")
    os.makedirs(docs_dir, exist_ok=True)

    # 场次文件（早报/晚报独立保存，唯一真实内容副本）
    session_name = f"report_{today}_{session_slug}.html"
    session_path = os.path.join(docs_dir, session_name)
    with open(session_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    # index.html → 最新报告
    with open(os.path.join(docs_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_content)

    # 主文件（无后缀，向后兼容）→ 跳转桩，指向当日场次文件
    report_path = os.path.join(docs_dir, f"report_{today}.html")
    stub = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\">\n"
        f"<meta http-equiv=\"refresh\" content=\"0; url={session_name}\">\n"
        "<meta name=\"robots\" content=\"noindex\">\n"
        f"<title>MARKET BRIEF · {today} · 跳转中</title>\n</head>\n<body>\n"
        f"<p>正在打开今日报告……如未自动跳转，请点击 <a href=\"{session_name}\">此处</a>。</p>\n"
        "</body>\n</html>\n"
    )
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(stub)

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

    archive_dir = os.path.join(_BASE_DIR, "docs")
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
        if not session:
            # 无后缀主文件现为跳转桩，不再是独立报告，归档里不单列一行
            continue
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
            "label": "早报" if session == "am" else "晚报",
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
    # 场次由【触发的 cron】决定（不受 GitHub Actions 调度延迟影响），
    # 报告归属时间在跨午夜延迟运行时回退一天，避免晚报被写成次日 _am。
    session_label, session_slug = get_session_label()
    report_time = get_report_time(session_slug)
    today_str = report_time.strftime("%Y%m%d")
    actual_now = beijing_now()
    delay_note = ""
    if report_time.date() != actual_now.date():
        delay_note = (f" ⚠ 调度延迟：本子属于 {report_time.strftime('%Y-%m-%d')} {session_label}，"
                      f"实际执行于 {actual_now.strftime('%Y-%m-%d %H:%M')}")
    print(f"[{actual_now}] 开始生成每日市场情报（{session_label} · 报告日期 {report_time.strftime('%Y-%m-%d')}）...{delay_note}")
    print()

    # 周末 / 法定休市日判断（周六全天、周日早报、节假日表命中 → 跳过）；按归属日期判断
    early_reason = get_skip_reason(None, session_label, now=report_time)
    if early_reason:
        _abort_run(early_reason)
        return

    # 防重复：自动 cron 触发时，如本场次报告已存在则跳过（手动触发不受限制）
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "schedule":
        existing_report = os.path.join(_BASE_DIR, "docs", f"report_{today_str}_{session_slug}.html")
        if os.path.exists(existing_report):
            _abort_run(f"本场次报告已存在（report_{today_str}_{session_slug}.html），跳过重复生成；"
                       "如需重新生成请在 Actions 页面手动触发")
            return

    # 1. 行情
    print("▸ 抓取指数行情...")
    quotes = fetch_index_quotes()
    for q in quotes:
        print(f"  {q['name']}: {q['price']} ({q['change']})")

    # 休市二次判断：用行情自带的最新交易日自证（兜住节假日表漏配）
    late_reason = get_skip_reason(quotes, session_label, now=report_time)
    if late_reason:
        _abort_run(late_reason)
        return

    # 2. 新闻 + 资金面 + 情绪硬指标（并行，互不依赖）
    print("\n▸ 抓取多市场新闻 & 资金面数据...")
    # 涨停/跌停池必须按【最近交易日】查询：早报在开盘前运行，当天尚无池数据
    trade_date_label = next((q.get("trade_date") for q in quotes if q.get("trade_date")), "")
    sent_date = trade_date_label.replace("-", "") or report_time.strftime("%Y%m%d")
    with ThreadPoolExecutor(max_workers=2) as executor:
        news_future = executor.submit(fetch_all_news)
        sentiment_future = executor.submit(fetch_market_sentiment, sent_date)
        news_list, fund_flow, news_errors = news_future.result()
        sentiment = sentiment_future.result()
    if sentiment:
        print(f"  情绪指标: 已获取 {list(sentiment.keys())}")
    else:
        print("  情绪指标: 未获取（本次报告不含该板块）")

    a_news = [n for n in news_list if n.get("market") == "A股" and "error" not in n]
    us_news = [n for n in news_list if n.get("market") == "美股" and "error" not in n]
    hk_news = [n for n in news_list if n.get("market") == "港股" and "error" not in n]
    print(f"  A股: {len(a_news)}条 | 美股: {len(us_news)}条 | 港股: {len(hk_news)}条")
    if fund_flow:
        latest = fund_flow[-1]
        direction = "流入" if latest["net_flow"] >= 0 else "流出"
        print(f"  主力资金({latest['date']}): {direction} {abs(latest['net_flow']):.1f}亿")

    if not a_news and not us_news and not hk_news:
        # 标记跳过：否则 workflow 仍会走提交 + 部署，空跑一次且无任何产出
        _abort_run(f"未抓取到任何新闻（接口错误 {len(news_errors)} 个），跳过本次生成")
        return

    # 3. 格式化 & LLM 分析
    news_text = format_news(news_list, fund_flow, sentiment, trade_date_label)
    print("\n▸ 生成 AI 市场分析...")
    report, analyst_status = call_llm(news_text)

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
    all_files = find_today_opinions(date_offset=date_offset, base_date=report_time)
    opinion_files = [o for o in all_files if o["kind"] == "opinion"]
    info_files = [o for o in all_files if o["kind"] == "info"]

    opinion_context = ""   # UP主蒸馏结果，传给选股
    info_context = ""      # 信息差原始内容，传给选股
    opinion_md = ""        # UP主蒸馏 markdown，追加到 report
    info_md = ""           # 信息差提炼结果
    opinion_status = "无输入"   # 运行摘要用
    info_status = "无输入"

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
                    opinion_status = "正常"
                    print("  观点蒸馏完成")
                else:
                    print(f"  观点分析失败: {opinion_md[:100] if opinion_md else '无返回'}")
                    opinion_md = ""
                    # 降级：直接展示原始观点文本，避免内容丢失
                    if combined_text:
                        opinion_context = f"## UP主市场观点（原文）\n\n{combined_text}"
                        report += f"\n\n---\n\n{opinion_md_title}\n\n{combined_text}"
                        opinion_status = "降级（展示原文）"
                        print("  已降级展示UP主观点原文")

            if info_future:
                info_md = info_future.result()
                if info_md and "暂时不可用" not in info_md:
                    info_context = f"## 信息差提炼（AI 提取关键事实）\n\n{info_md}"
                    report += f"\n\n---\n\n## 信息差提炼\n\n{info_md}"
                    info_status = "正常"
                    print("  信息差提炼完成")
                else:
                    print(f"  信息差提炼失败: {info_md[:100] if info_md else '无返回'}")
                    info_context = f"## 信息差补充\n\n{info_raw}"
                    report += f"\n\n---\n\n## 信息差补充\n\n{info_raw}"
                    info_status = "降级（展示原文）"

    # 5. AI 选股 — 融合新闻 + UP主观点 + 信息差
    print("\n▸ 执行 AI 产业链选股（融合新闻+观点+信息差）...")
    stock_picks = call_stock_picker(news_text,
                                    opinion_context=opinion_context,
                                    info_context=info_context)
    picks_status = "降级（未发布草稿）" if stock_picks == PICKS_FALLBACK else "正常"

    # 预先构造 GitHub Pages URL（today_str 已在开头按报告归属日期算好）
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        owner = repo.split("/")[0].lower()
        repo_name = repo.split("/")[1]
        page_base_url = f"https://{owner}.github.io/{repo_name}/"
    else:
        page_base_url = "https://hcongxi42-web.github.io/HZT/"

    # 6. 清理旧文件
    cleanup_old_files(days=7)


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
                                fund_flow, session_label=session_label, session_slug=session_slug,
                                up_count=len(opinion_files), report_time=report_time)
    page_url = deploy_github_pages(html, session_slug=session_slug)

    # 7. PDF
    print("▸ 生成 PDF...")
    # 必须渲染真实内容页：无后缀主文件已改为跳转桩，直接打印只会得到空白/跳转页
    html_file = os.path.join(_BASE_DIR, "docs", f"report_{today_str}_{session_slug}.html")
    pdf_path = generate_pdf(html_file)

    # 8. 保存 Markdown
    report_file = os.path.join(_BASE_DIR, f"report_{beijing_now().strftime('%Y%m%d_%H%M')}.md")
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

    # 10. 运行摘要（stdout + Actions 运行页），各节降级一眼可见
    # 情绪指标摘要文案
    sent_note = ""
    if sentiment.get("breadth"):
        _b = sentiment["breadth"]
        sent_note = f"涨{_b['up']}/跌{_b['down']} · 成交{_b['turnover_yi']:.0f}亿"
    if sentiment.get("limit"):
        _l = sentiment["limit"]
        _extra = []
        if _l.get("zt") is not None:
            _extra.append(f"涨停{_l['zt']}")
        if _l.get("dt") is not None:
            _extra.append(f"跌停{_l['dt']}")
        if _extra:
            sent_note = (sent_note + " · " if sent_note else "") + " · ".join(_extra)

    # 样本不足提醒（<5 条的市场单独标出）
    thin = [n for n, c in (("A股", len(a_news)), ("美股", len(us_news)), ("港股", len(hk_news))) if c < 5]

    write_run_summary([
        ("运行场次", f"{session_label} · {report_time.strftime('%Y-%m-%d')}",
         delay_note.strip(" ⚠") if delay_note else f"实际 {actual_now.strftime('%m-%d %H:%M')}"),
        ("指数行情", f"{sum(1 for q in quotes if q['price'] != '--')}/{len(quotes)} 条有效", ""),
        ("新闻 A/美/港", f"{len(a_news)} / {len(us_news)} / {len(hk_news)} 条",
         ("接口错误 %d 个；" % len(news_errors) if news_errors else "")
         + (f"样本不足: {'/'.join(thin)}" if thin else "")),
        ("情绪硬指标", "正常" if sentiment else "无数据（本节省略）", sent_note),
        ("资金面（沪深300）", "正常" if fund_flow else "无数据（面板隐藏）",
         fund_flow[-1]["date"] if fund_flow else ""),
        ("盘面分析", "正常" if analyst_status == "ok" else "降级（未发布草稿）", ""),
        ("UP主观点", opinion_status, f"匹配 {len(opinion_files)} 位" if opinion_files else "无文件"),
        ("信息差", info_status, f"匹配 {len(info_files)} 条" if info_files else "无文件"),
        ("AI选股", picks_status, ""),
        ("PDF", "已生成" if pdf_path else "跳过（本地无 Chrome 或失败）", ""),
        ("产出", f"report_{today_str}_{session_slug}.html + index.html", page_url),
    ])

    print(f"\n[{beijing_now()}] 完成")


if __name__ == "__main__":
    main()
