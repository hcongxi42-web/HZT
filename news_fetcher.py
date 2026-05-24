"""
股市新闻抓取器 - 作为 Dify 自定义工具的后端服务
支持 A股、美股、港股新闻抓取
"""

import json
import re
import urllib.request
import urllib.error
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler


def fetch_cls_news(market_filter=None):
    """从财联社抓取快讯

    market_filter: None=全部, 'A'=A股, 'HK'=港股
    """
    url = "https://www.cls.cn/nodeapi/updateTelegraphList?app=CailianpressWeb&os=web&sv=8.4.6&rn=50"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.cls.cn/",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        articles = []
        for item in data.get("data", {}).get("roll_data", []):
            content = item.get("content", "")
            # 去除 HTML 标签
            content = re.sub(r"<[^>]+>", "", content)
            ctime = item.get("ctime", 0)
            time_str = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M") if ctime else ""

            # 根据 subjects 和内容判断市场类型
            subjects = item.get("subjects", [])
            subject_names = [s.get("subject_name", "") for s in subjects]
            subject_str = " ".join(subject_names)

            is_hk = any(kw in subject_str for kw in ["港股", "港交所", "HK"]) or \
                     any(kw in content for kw in ["港股", "恒生", "港交所", "恒指"])
            is_us = any(kw in subject_str for kw in ["美股", "美联储", "纳斯达克"]) or \
                    any(kw in content for kw in ["美股", "纳指", "标普", "道指"])

            if market_filter == "HK" and not is_hk:
                continue
            if market_filter == "A" and (is_hk or is_us):
                continue

            market_label = "港股" if is_hk else ("美股" if is_us else "A股")

            title = item.get("title", "")
            if not title:
                title = content[:50]

            articles.append({
                "title": title,
                "summary": content[:200],
                "time": time_str,
                "source": "财联社",
                "market": market_label,
            })

            if len(articles) >= 15:
                break

        return articles
    except Exception as e:
        market_name = {"HK": "港股", "A": "A股"}.get(market_filter, "综合")
        return [{"error": f"财联社{market_name}抓取失败: {str(e)}", "source": "财联社", "market": market_name}]


def fetch_eastmoney_news():
    """从东方财富 np-listapi 抓取 A股综合财经新闻"""
    url = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&column=350&order=1&needInteractData=0&page_index=1&page_size=15&req_trace=a"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://finance.eastmoney.com/",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        articles = []
        for item in data.get("data", {}).get("list", [])[:15]:
            articles.append({
                "title": item.get("title", ""),
                "summary": item.get("digest", "")[:200],
                "time": item.get("showTime", ""),
                "source": "东方财富",
                "market": "A股",
            })
        return articles
    except Exception as e:
        return [{"error": f"东方财富抓取失败: {str(e)}", "source": "东方财富", "market": "A股"}]


def fetch_eastmoney_kuaixun(type_id="102", market="A股", count=15):
    """从东方财富快讯 API 抓取新闻

    type_id:
      102 = 全部快讯(偏A股和综合)
      103 = A股公告/要闻
      105 = 全球要闻
      110 = A股公司新闻
      111 = 美股快讯
    """
    url = f"https://newsapi.eastmoney.com/kuaixun/v1/getlist_{type_id}_ajaxResult_{count}_1_.html"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://finance.eastmoney.com/",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            # Strip JSONP wrapper: var ajaxResult={...}
            json_str = raw[raw.index("{"):raw.rindex("}") + 1]
            data = json.loads(json_str)

        articles = []
        for item in data.get("LivesList", [])[:15]:
            articles.append({
                "title": item.get("title", ""),
                "summary": item.get("digest", item.get("title", ""))[:200],
                "time": item.get("showtime", ""),
                "source": "东方财富",
                "market": market,
            })
        return articles
    except Exception as e:
        return [{"error": f"东方财富快讯({market})抓取失败: {str(e)}", "source": "东方财富", "market": market}]


def fetch_sina_us_stock():
    """从新浪财经抓取美股新闻"""
    url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num=15&page=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://finance.sina.com.cn/",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        articles = []
        for item in data.get("result", {}).get("data", [])[:15]:
            title = item.get("title", "")
            # 去除 HTML 实体
            title = title.replace("&nbsp;", " ").replace("&amp;", "&")
            title = re.sub(r"<[^>]+>", "", title)
            ctime = item.get("ctime", "")
            if ctime:
                try:
                    time_str = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    time_str = ctime
            else:
                time_str = ""
            intro = item.get("intro", "")
            intro = re.sub(r"<[^>]+>", "", intro)
            articles.append({
                "title": title,
                "summary": (intro or title)[:200],
                "time": time_str,
                "source": "新浪财经",
                "market": "美股",
            })
        return articles
    except Exception as e:
        return [{"error": f"新浪美股抓取失败: {str(e)}", "source": "新浪财经", "market": "美股"}]


def fetch_eastmoney_hk():
    """从东方财富抓取港股相关新闻 (快讯 + 列表 API)"""
    # 使用 np-listapi column=351 (全球/港股综合快讯)
    url = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&column=351&order=1&needInteractData=0&page_index=1&page_size=15&req_trace=a"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://hk.eastmoney.com/",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        articles = []
        for item in data.get("data", {}).get("list", [])[:15]:
            articles.append({
                "title": item.get("title", ""),
                "summary": item.get("digest", "")[:200],
                "time": item.get("showTime", ""),
                "source": "东方财富",
                "market": "港股",
            })
        return articles
    except Exception as e:
        return [{"error": f"东方财富港股抓取失败: {str(e)}", "source": "东方财富", "market": "港股"}]


def fetch_all_news(market="all"):
    """抓取所有市场新闻"""
    results = {"date": datetime.now().strftime("%Y-%m-%d %H:%M"), "news": []}

    if market in ("all", "a"):
        results["news"].extend(fetch_cls_news(market_filter="A"))
        results["news"].extend(fetch_eastmoney_news())
    if market in ("all", "us"):
        results["news"].extend(fetch_sina_us_stock())
        results["news"].extend(fetch_eastmoney_kuaixun(type_id="111", market="美股"))
    if market in ("all", "hk"):
        results["news"].extend(fetch_cls_news(market_filter="HK"))
        results["news"].extend(fetch_eastmoney_hk())

    return results


class NewsHandler(BaseHTTPRequestHandler):
    """HTTP 服务，供 Dify 通过 HTTP Request 节点调用"""

    def do_GET(self):
        # 解析 market 参数
        market = "all"
        if "?" in self.path:
            params = dict(p.split("=") for p in self.path.split("?")[1].split("&") if "=" in p)
            market = params.get("market", "all")

        news = fetch_all_news(market)
        response = json.dumps(news, ensure_ascii=False, indent=2)

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(response.encode("utf-8"))

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


if __name__ == "__main__":
    # 可直接运行测试
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        news = fetch_all_news()
        print(json.dumps(news, ensure_ascii=False, indent=2))
        # 输出统计
        a_count = sum(1 for n in news["news"] if n.get("market") == "A股" and "error" not in n)
        us_count = sum(1 for n in news["news"] if n.get("market") == "美股" and "error" not in n)
        hk_count = sum(1 for n in news["news"] if n.get("market") == "港股" and "error" not in n)
        err_count = sum(1 for n in news["news"] if "error" in n)
        print(f"\n--- 统计 ---")
        print(f"A股新闻: {a_count} 条")
        print(f"美股新闻: {us_count} 条")
        print(f"港股新闻: {hk_count} 条")
        print(f"错误: {err_count} 条")
        print(f"总计: {len(news['news'])} 条")
    else:
        port = 8766
        server = HTTPServer(("0.0.0.0", port), NewsHandler)
        print(f"股市新闻服务已启动: http://localhost:{port}")
        print(f"  全部新闻: http://localhost:{port}/news?market=all")
        print(f"  A股新闻:  http://localhost:{port}/news?market=a")
        print(f"  美股新闻: http://localhost:{port}/news?market=us")
        print(f"  港股新闻: http://localhost:{port}/news?market=hk")
        server.serve_forever()
