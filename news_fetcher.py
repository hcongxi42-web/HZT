"""
股市新闻与资金面数据抓取器
- 多市场新闻：A股 / 美股 / 港股
- 资金面：北向资金 / 大盘资金流向
- 支持 Dify HTTP 服务模式 + 独立测试模式

数据源优先级：东方财富快讯（全文） > 东方财富列表 > 新浪财经
"""

import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler


# ============================================================
#  通用工具
# ============================================================

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_REFERER_EM = "https://finance.eastmoney.com/"
_REFERER_SINA = "https://finance.sina.com.cn/"


def _jsonp_strip(raw):
    """Remove JSONP wrapper, return parsed dict."""
    s = raw.strip()
    # jQuery1234567890_1234567890({...})
    if s.startswith("jQuery"):
        s = s[s.index("(") + 1 : s.rindex(")")]
    # var xxx={...}
    if s.startswith("var "):
        s = s[s.index("{") : s.rindex("}") + 1]
    return json.loads(s)


def _clean_html(text):
    """Strip HTML tags from text."""
    return re.sub(r"<[^>]+>", "", text or "")


def _dedup_news(articles, key_fn=None):
    """Remove duplicate articles by title similarity."""
    if key_fn is None:
        key_fn = lambda a: a.get("title", "")[:40]
    seen = set()
    result = []
    for a in articles:
        k = key_fn(a)
        if k not in seen:
            seen.add(k)
            result.append(a)
    return result


def _retry_fetch(fn, *args, max_retries=3, **kwargs):
    """Retry wrapper for flaky API calls with exponential backoff."""
    import time
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn(*args, **kwargs)
        except (urllib.error.URLError, ConnectionResetError, TimeoutError) as e:
            last_err = e
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 2
                time.sleep(wait)
        except Exception as e:
            # Non-retryable errors
            raise e
    raise last_err


# ============================================================
#  A 股新闻
# ============================================================

def fetch_em_kuaixun(type_id="102", market="A股", count=20):
    """从东方财富快讯 API 抓取新闻（**主力源**，含完整摘要）。

    type_id:
      102 = 全部快讯（A股+综合）
      103 = A股公告/要闻
      105 = 全球要闻
      110 = A股公司新闻
      111 = 美股快讯
    """
    url = (
        f"https://newsapi.eastmoney.com/kuaixun/v1/"
        f"getlist_{type_id}_ajaxResult_{count}_1_.html"
    )
    headers = {"User-Agent": _UA, "Referer": _REFERER_EM}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            # JSONP wrapper
            json_str = raw[raw.index("{") : raw.rindex("}") + 1]
            data = json.loads(json_str)

        articles = []
        for item in data.get("LivesList", [])[:count]:
            title = _clean_html(item.get("title", ""))
            digest = _clean_html(item.get("digest", ""))
            # Fallback: sometimes digest equals title → use title only
            if digest == title:
                digest = ""
            showtime = item.get("showtime", "")
            articles.append({
                "title": title,
                "summary": digest[:300] if digest else title[:300],
                "time": showtime,
                "source": "东方财富",
                "market": market,
                "type": "快讯",
            })
        return articles
    except Exception as e:
        return [{"error": f"东方财富快讯({market})抓取失败: {e}", "source": "东方财富", "market": market}]


def fetch_em_news_list(column="350", count=15):
    """从东方财富 np-listapi 抓取栏目新闻（辅助源）。"""
    url = (
        f"https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        f"?client=web&biz=web_news_col&column={column}&order=1"
        f"&needInteractData=0&page_index=1&page_size={count}&req_trace=a"
    )
    headers = {"User-Agent": _UA, "Referer": _REFERER_EM}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        articles = []
        for item in data.get("data", {}).get("list", [])[:count]:
            title = _clean_html(item.get("title", ""))
            digest = _clean_html(item.get("digest", ""))
            articles.append({
                "title": title,
                "summary": digest[:300] if digest else title[:300],
                "time": item.get("showTime", ""),
                "source": "东方财富",
                "market": "A股",
                "type": "要闻",
            })
        return articles
    except Exception as e:
        return [{"error": f"东方财富要闻抓取失败: {e}", "source": "东方财富", "market": "A股"}]


def fetch_sina_a_news(count=10):
    """从新浪财经抓取 A 股滚动新闻（辅助源）。"""
    url = (
        f"https://feed.mix.sina.com.cn/api/roll/get"
        f"?pageid=153&lid=2512&k=&num={count}&page=1"
    )
    headers = {"User-Agent": _UA, "Referer": _REFERER_SINA}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        articles = []
        for item in data.get("result", {}).get("data", [])[:count]:
            title = _clean_html(
                item.get("title", "")
                .replace("&nbsp;", " ")
                .replace("&amp;", "&")
            )
            intro = _clean_html(item.get("intro", ""))
            ctime = item.get("ctime", "")
            if ctime:
                try:
                    time_str = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    time_str = ctime
            else:
                time_str = ""

            # 过滤非财经内容
            finance_kw = ["股", "市", "基金", "A股", "IPO", "涨", "跌", "板块",
                          "指数", "公司", "业绩", "公告", "分红", "并购", "重组",
                          "央行", "证监会", "政策", "经济", "行业", "投资"]
            combined = title + intro
            if not any(kw in combined for kw in finance_kw):
                continue

            articles.append({
                "title": title,
                "summary": (intro or title)[:300],
                "time": time_str,
                "source": "新浪财经",
                "market": "A股",
                "type": "滚动",
            })
        return articles
    except Exception as e:
        return [{"error": f"新浪A股抓取失败: {e}", "source": "新浪财经", "market": "A股"}]


def fetch_a_stock_news():
    """聚合所有 A 股新闻源，去重后返回。"""
    all_articles = []

    # 主力：东方财富快讯（全文摘要）
    all_articles.extend(fetch_em_kuaixun("102", "A股", 20))   # 全部快讯
    all_articles.extend(fetch_em_kuaixun("103", "A股", 10))   # 公告要闻
    all_articles.extend(fetch_em_kuaixun("110", "A股", 10))   # 公司新闻

    # 辅助：栏目新闻
    all_articles.extend(fetch_em_news_list("350", 10))        # 要闻栏目

    # 补充：新浪财经
    all_articles.extend(fetch_sina_a_news(10))

    # 去重
    valid = [a for a in all_articles if "error" not in a]
    errors = [a for a in all_articles if "error" in a]
    deduped = _dedup_news(valid)

    return deduped, errors


# ============================================================
#  美股新闻
# ============================================================

def fetch_sina_us_stock(count=15):
    """从新浪财经抓取美股新闻。"""
    url = (
        f"https://feed.mix.sina.com.cn/api/roll/get"
        f"?pageid=153&lid=2509&k=&num={count}&page=1"
    )
    headers = {"User-Agent": _UA, "Referer": _REFERER_SINA}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        articles = []
        for item in data.get("result", {}).get("data", [])[:count]:
            title = _clean_html(
                item.get("title", "")
                .replace("&nbsp;", " ")
                .replace("&amp;", "&")
            )
            intro = _clean_html(item.get("intro", ""))
            ctime = item.get("ctime", "")
            if ctime:
                try:
                    time_str = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    time_str = ctime
            else:
                time_str = ""
            articles.append({
                "title": title,
                "summary": (intro or title)[:300],
                "time": time_str,
                "source": "新浪财经",
                "market": "美股",
                "type": "滚动",
            })
        return articles
    except Exception as e:
        return [{"error": f"新浪美股抓取失败: {e}", "source": "新浪财经", "market": "美股"}]


def fetch_us_stock_news():
    """聚合美股新闻源。"""
    all_articles = []
    all_articles.extend(fetch_sina_us_stock(15))
    all_articles.extend(fetch_em_kuaixun("111", "美股", 10))   # 美股快讯
    all_articles.extend(fetch_em_kuaixun("105", "美股", 8))    # 全球要闻

    valid = [a for a in all_articles if "error" not in a]
    errors = [a for a in all_articles if "error" in a]
    deduped = _dedup_news(valid)
    return deduped, errors


# ============================================================
#  港股新闻
# ============================================================

def fetch_em_hk_news(count=15):
    """从东方财富抓取港股新闻。"""
    url = (
        f"https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        f"?client=web&biz=web_news_col&column=351&order=1"
        f"&needInteractData=0&page_index=1&page_size={count}&req_trace=a"
    )
    headers = {"User-Agent": _UA, "Referer": "https://hk.eastmoney.com/"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        articles = []
        for item in data.get("data", {}).get("list", [])[:count]:
            title = _clean_html(item.get("title", ""))
            digest = _clean_html(item.get("digest", ""))
            articles.append({
                "title": title,
                "summary": digest[:300] if digest else title[:300],
                "time": item.get("showTime", ""),
                "source": "东方财富",
                "market": "港股",
                "type": "要闻",
            })
        return articles
    except Exception as e:
        return [{"error": f"东方财富港股抓取失败: {e}", "source": "东方财富", "market": "港股"}]


def fetch_hk_stock_news():
    """聚合港股新闻源。"""
    all_articles = []
    all_articles.extend(fetch_em_hk_news(15))
    all_articles.extend(fetch_em_kuaixun("105", "港股", 8))   # 全球要闻

    valid = [a for a in all_articles if "error" not in a]
    errors = [a for a in all_articles if "error" in a]
    deduped = _dedup_news(valid)
    return deduped, errors


# ============================================================
#  资金面数据 — 北向资金
# ============================================================

def fetch_northbound_flow(days=5):
    """获取北向资金（沪深港通）每日数据。

    返回格式:
      [{"date": "2026-06-05", "net_buy_sh": 12.34, "net_buy_sz": 5.67, "net_buy_total": 18.01}, ...]

    注意：非交易日/数据未更新时可能返回零值。
    数据来源：东方财富 push2his K 线 API。
    """
    url = (
        "https://push2his.eastmoney.com/api/qt/kamt.kline/get"
        "?fields1=f1,f2,f3,f7"
        "&fields2=f51,f52,f53,f54"
        "&klt=101&lmt=30"
        "&secid=0.000001"
    )
    headers = {"User-Agent": _UA}

    def _do_fetch():
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        result = []
        hk2sh = data.get("data", {}).get("hk2sh", [])
        hk2sz = data.get("data", {}).get("hk2sz", [])

        # 每行格式: date,net_buy,quota_remaining,cumulative_balance
        # 数值单位可能为万元或原始元，按比例推算
        for sh_row, sz_row in zip(hk2sh[-days:], hk2sz[-days:]):
            sh_parts = sh_row.split(",")
            sz_parts = sz_row.split(",")
            try:
                # 东方财富 f52 为当日净买入（原始值）
                net_sh_raw = float(sh_parts[1]) if len(sh_parts) > 1 else 0
                net_sz_raw = float(sz_parts[1]) if len(sz_parts) > 1 else 0
                # 尝试检测单位：如果日配额约 5,200,000，则单位为百元
                quota_raw = float(sh_parts[2]) if len(sh_parts) > 2 else 0
                if quota_raw > 100000:  # 大数→单位可能是元，除以1e8得亿
                    divisor = 1e8
                elif quota_raw > 1000:  # 中等→单位可能是万元
                    divisor = 1e4
                else:
                    divisor = 1  # 已是亿元单位

                net_sh = round(net_sh_raw / divisor, 2)
                net_sz = round(net_sz_raw / divisor, 2)
            except (ValueError, IndexError):
                net_sh = 0.0
                net_sz = 0.0

            result.append({
                "date": sh_parts[0],
                "net_buy_sh": net_sh,
                "net_buy_sz": net_sz,
                "net_buy_total": round(net_sh + net_sz, 2),
            })
        return result

    try:
        result = _retry_fetch(_do_fetch)
        # 检测数据是否有效：最近3天至少有一天非零
        recent_nonzero = any(
            abs(r.get("net_buy_total", 0)) > 0.01 for r in result[-3:]
        )
        if not recent_nonzero:
            print("[Northbound] 近3日数据均为零，可能非交易日或数据源延迟")
        return result
    except Exception as e:
        print(f"[Northbound] 获取失败 (已重试): {e}")
        return []


# ============================================================
#  资金面数据 — 大盘资金流向
# ============================================================

def fetch_market_fund_flow(days=5):
    """获取全市场主力资金流向（沪深两市合计）。

    返回格式:
      [{"date": "2026-06-05", "net_flow": -47.20, "main_in": 332.55, "main_out": 379.75}, ...]

    kline 格式: date,主力净流入,超大单净流入,大单净流入,中单净流入,小单净流入 (单位：元)
    secid=1.000300 为沪深300资金流向（股票+ETF合计）
    """
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        f"?secid=1.000300"  # 沪深300
        f"&fields1=f1,f2,f3,f7"
        f"&fields2=f51,f52,f53,f54,f55"
        f"&klt=101&lmt=30"
    )
    headers = {"User-Agent": _UA}

    def _do_fetch():
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        result = []
        klines = data.get("data", {}).get("klines", [])
        for row in klines[-days:]:
            parts = row.split(",")
            if len(parts) >= 5:
                try:
                    super_large = float(parts[1]) / 1e8  # 超大单净流入
                    large = float(parts[2]) / 1e8         # 大单净流入
                    medium = float(parts[3]) / 1e8        # 中单净流入
                    small = float(parts[4]) / 1e8         # 小单净流入
                    net_flow = super_large + large        # 主力 = 超大单 + 大单
                    main_in = max(0, super_large) + max(0, large)
                    main_out = abs(min(0, super_large)) + abs(min(0, large))
                except (ValueError, IndexError):
                    continue
                result.append({
                    "date": parts[0],
                    "net_flow": round(net_flow, 2),
                    "main_in": round(main_in, 2),
                    "main_out": round(main_out, 2),
                    "super_large": round(super_large, 2),
                    "large": round(large, 2),
                    "medium": round(medium, 2),
                    "small": round(small, 2),
                })
        return result

    try:
        return _retry_fetch(_do_fetch)
    except Exception as e:
        print(f"[FundFlow] 大盘资金流向获取失败 (已重试): {e}")
        return []


# ============================================================
#  聚合入口
# ============================================================

def fetch_all_news(market="all"):
    """统一聚合入口：按市场抓取新闻 + 资金面数据。

    Returns dict:
      {
        "date": "2026-06-07 16:30",
        "markets": {
          "a":  {"news": [...], "errors": [...]},
          "us": {"news": [...], "errors": [...]},
          "hk": {"news": [...], "errors": [...]},
        },
        "northbound": [...],    # 北向资金
        "fund_flow": [...],     # 大盘资金流向
      }
    """
    result = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "markets": {},
    }

    if market in ("all", "a"):
        news, errors = fetch_a_stock_news()
        result["markets"]["a"] = {"news": news, "errors": errors}

    if market in ("all", "us"):
        news, errors = fetch_us_stock_news()
        result["markets"]["us"] = {"news": news, "errors": errors}

    if market in ("all", "hk"):
        news, errors = fetch_hk_stock_news()
        result["markets"]["hk"] = {"news": news, "errors": errors}

    # 资金面数据（始终抓取）
    result["northbound"] = fetch_northbound_flow(5)
    result["fund_flow"] = fetch_market_fund_flow(5)

    return result


def fetch_all_news_flat(market="all"):
    """扁平化聚合：将所有市场的有效新闻合为一个列表。

    用于 stock_report.py 等只需要新闻正文的场景。
    """
    data = fetch_all_news(market)
    all_articles = []
    all_errors = []

    for mkt in data.get("markets", {}).values():
        all_articles.extend(mkt.get("news", []))
        all_errors.extend(mkt.get("errors", []))

    # 跨市场去重
    all_articles = _dedup_news(all_articles)

    return all_articles, all_errors, data.get("northbound", []), data.get("fund_flow", [])


# ============================================================
#  HTTP 服务（Dify 集成用）
# ============================================================

class NewsHandler(BaseHTTPRequestHandler):
    """HTTP 服务，供 Dify 通过 HTTP Request 节点调用。"""

    def do_GET(self):
        market = "all"
        if "?" in self.path:
            params = dict(
                p.split("=") for p in self.path.split("?")[1].split("&") if "=" in p
            )
            market = params.get("market", "all")

        data = fetch_all_news(market)
        response = json.dumps(data, ensure_ascii=False, indent=2)

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(response.encode("utf-8"))

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


# ============================================================
#  独立测试入口
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("=" * 60)
        print("  股市新闻 & 资金面数据抓取测试")
        print("=" * 60)
        print()

        data = fetch_all_news("all")

        # 新闻统计
        for mkt_key, mkt_name in [("a", "A股"), ("us", "美股"), ("hk", "港股")]:
            mkt = data["markets"].get(mkt_key, {})
            news = mkt.get("news", [])
            errors = mkt.get("errors", [])
            print(f"--- {mkt_name} ---")
            print(f"  有效: {len(news)} 条, 错误: {len(errors)} 个")
            for a in news[:3]:
                print(f"  [{a.get('time','?')}] [{a.get('source','?')}] {a.get('title','')[:80]}")
            if len(news) > 3:
                print(f"  ... 还有 {len(news) - 3} 条")
            print()

        # 北向资金
        print("--- 北向资金（近5日）---")
        nb = data.get("northbound", [])
        for row in nb:
            direction = "净流入" if row["net_buy_total"] >= 0 else "净流出"
            print(f"  {row['date']}  北向{direction} {abs(row['net_buy_total']):.2f} 亿元  (沪:{row['net_buy_sh']:+.2f}  深:{row['net_buy_sz']:+.2f})")
        if not nb:
            print("  (今日非交易日或无数据)")
        print()

        # 大盘资金
        print("--- 大盘主力资金（近5日）---")
        ff = data.get("fund_flow", [])
        for row in ff:
            direction = "净流入" if row["net_flow"] >= 0 else "净流出"
            print(f"  {row['date']}  主力{direction} {abs(row['net_flow']):.2f} 亿元  (流入:{row['main_in']:.2f}  流出:{row['main_out']:.2f})")
        if not ff:
            print("  (暂无数据)")
        print()

        # 总计
        total_news = sum(len(m["news"]) for m in data["markets"].values())
        total_errs = sum(len(m["errors"]) for m in data["markets"].values())
        print(f"总计: {total_news} 条有效新闻, {total_errs} 个接口错误")

    else:
        port = 8766
        server = HTTPServer(("0.0.0.0", port), NewsHandler)
        print(f"股市新闻服务已启动: http://localhost:{port}")
        print(f"  全部: http://localhost:{port}/news?market=all")
        print(f"  A股:  http://localhost:{port}/news?market=a")
        print(f"  美股: http://localhost:{port}/news?market=us")
        print(f"  港股: http://localhost:{port}/news?market=hk")
        server.serve_forever()
