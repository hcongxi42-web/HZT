# -*- coding: utf-8 -*-
"""回归测试：把线上踩过的坑固化成断言（纯标准库，CI 无需安装任何依赖）。

运行方式：
    python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import news_fetcher
import stock_report as sr


class TestFundFlowColumns(unittest.TestCase):
    """资金流列序回归。

    2026-09 线上事故：接口真实列序为
      [1]=主力净流入 [2]=小单 [3]=中单 [4]=大单 [5]=超大单，
    旧代码按 [1]=超大单 [2]=大单 解析，算出 net_flow = 主力 + 小单，
    导致显示的"主力资金"严重失真（如 -129.8 亿被算成 -0.9 亿）。
    下面用当天真实响应锁定正确口径。
    """

    REAL_KLINES = [
        "2026-09-02,-28702580736.0,20267040768.0,8435535872.0,-10245304320.0,-18457276416.0",
        "2026-09-03,-9362395136.0,8476798976.0,885600256.0,-2324086784.0,-7038308352.0",
        "2026-09-04,-12982153216.0,12063080448.0,919064576.0,-1765343232.0,-11216809984.0",
    ]

    def test_column_mapping(self):
        rows = news_fetcher.parse_fund_flow_klines(self.REAL_KLINES, days=3)
        self.assertEqual(len(rows), 3)
        last = rows[-1]
        self.assertEqual(last["date"], "2026-09-04")
        self.assertAlmostEqual(last["net_flow"], -129.82, places=2)      # 主力净流入
        self.assertAlmostEqual(last["super_large"], -112.17, places=2)   # 超大单
        self.assertAlmostEqual(last["large"], -17.65, places=2)          # 大单
        self.assertAlmostEqual(last["medium"], 9.19, places=2)           # 中单
        self.assertAlmostEqual(last["small"], 120.63, places=2)          # 小单

    def test_self_consistency(self):
        """两条恒等式：主力 == 超大单+大单；主力+小单+中单 == 0。"""
        for row in news_fetcher.parse_fund_flow_klines(self.REAL_KLINES, days=3):
            self.assertAlmostEqual(row["super_large"] + row["large"], row["net_flow"], places=1)
            self.assertAlmostEqual(row["net_flow"] + row["small"] + row["medium"], 0, places=1)

    def test_days_window_and_bad_rows(self):
        self.assertEqual(len(news_fetcher.parse_fund_flow_klines(self.REAL_KLINES, days=2)), 2)
        # 字段不足 / 非数字 → 跳过而不是抛异常
        self.assertEqual(news_fetcher.parse_fund_flow_klines(["2026-09-04,-1,2,3"], days=5), [])
        self.assertEqual(news_fetcher.parse_fund_flow_klines(["bad,x,y,z,a,b"], days=5), [])


class TestSinaQuoteParsing(unittest.TestCase):
    """行情解析回归：国际指数曾因取 parts[5]（不存在）导致涨跌幅恒为 0。"""

    def test_int_index_only_four_fields(self):
        q = sr._parse_sina_quote(
            "int_hangseng", "恒生指数",
            ["恒生指数", "25402.730", "-248.140", "-0.970"],
        )
        self.assertEqual(q["price"], "25402.73")
        self.assertEqual(q["change"], "-0.97%")   # 修复前恒为 +0.00%
        self.assertEqual(q["trade_date"], "")

    def test_a_share_index_with_trade_date(self):
        parts = (["上证指数", "3942.5093", "3930.1164", "3920.7015", "3948.4206", "3916.4915"]
                 + ["0"] * 24
                 + ["2026-09-07", "11:35:57", "00"])
        q = sr._parse_sina_quote("sh000001", "上证指数", parts)
        self.assertEqual(q["price"], "3920.70")
        self.assertEqual(q["change"], "-0.24%")
        self.assertEqual(q["trade_date"], "2026-09-07")   # 休市判断依赖该字段

    def test_unknown_code_raises(self):
        with self.assertRaises(ValueError):
            sr._parse_sina_quote("hk00700", "腾讯", ["腾讯", "1", "2"])


class TestNewsDedup(unittest.TestCase):
    """去重回归：旧实现只比对 title[:40]，标注/标点不同即漏去重。"""

    def test_normalized_title_collapses(self):
        arts = [
            {"title": "央行开展3000亿元MLF操作"},
            {"title": "央行开展3000亿元MLF操作！"},
            {"title": "【央行】央行开展 3000 亿元 MLF 操作"},
        ]
        self.assertEqual(len(news_fetcher._dedup_news(arts)), 1)

    def test_near_duplicate_collapses(self):
        arts = [
            {"title": "英伟达财报超预期 股价盘后大涨5%"},
            {"title": "英伟达财报超预期，股价盘后大涨6%"},
        ]
        self.assertEqual(len(news_fetcher._dedup_news(arts)), 1)

    def test_short_titles_not_fuzzy_merged(self):
        """短标题不做近似合并，避免「降准」被当成「降息」。"""
        arts = [{"title": "央行降准"}, {"title": "央行降息"}]
        self.assertEqual(len(news_fetcher._dedup_news(arts)), 2)

    def test_distinct_titles_kept(self):
        arts = [{"title": "半导体设备订单回暖"}, {"title": "光伏组件价格下跌"}]
        self.assertEqual(len(news_fetcher._dedup_news(arts)), 2)


class TestHtmlEscaping(unittest.TestCase):
    """注入面回归：第三方新闻标题可能经 LLM 回流进报告，必须转义。"""

    def test_injection_escaped(self):
        md = (
            "## 盘面分析\n\n"
            "- **市场体温**：偏暖 <script>alert('x')</script>\n\n"
            "| 股票及代码 | 选股逻辑 |\n|------|------|\n"
            "| 北方华创(002371) | <img src=x onerror=alert(2)> 订单回暖 |\n\n"
            "正常段落 <iframe src=\"evil\"> 与 **加粗要点** 保留。\n"
        )
        html = sr.markdown_to_html(md)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;iframe", html)
        self.assertIn("<strong>加粗要点</strong>", html)      # 正常 markdown 不受影响
        self.assertIn("北方华创(002371)", html)               # 表格内容不丢

    def test_quote_escaped(self):
        html = sr.markdown_to_html('段落 with "quotes" <b>raw</b>')
        self.assertNotIn("<b>", html)
        self.assertIn("&quot;quotes&quot;", html)


class TestSplitLead(unittest.TestCase):
    """今日要点抽取回归：仅支持带标题写法时，页面置顶摘要长期为空。"""

    def test_titled_form(self):
        md = "## 今日要点\n- **市场体温**：偏暖\n\n## 资金面\n正文"
        lead, rest = sr._split_lead(md, "今日要点")
        self.assertIn("市场体温", lead)
        self.assertIn("## 资金面", rest)

    def test_titleless_form(self):
        md = ("- **市场体温**：偏暖\n- **最大共识**：AI\n- **最大分歧**：券商\n"
              "- **最大风险**：加息\n- **一句话策略**：轻仓\n\n## 资金面\n正文")
        lead, rest = sr._split_lead(md, "今日要点")
        self.assertEqual(lead.count("- **"), 5)
        self.assertIn("## 资金面", rest)


class TestSanitizeStockPicks(unittest.TestCase):
    """选股输出校验回归：既要拦提示词回声，也不能误杀正常正文。"""

    LEGIT = (
        "### 半导体设备国产替代\n"
        "| 股票及代码 | 选股逻辑 | 方向 | 评级 |\n"
        "|------|------|------|------|\n"
        "| 北方华创(002371) | 设备订单回暖，国产替代直接受益 | 利好 | S级 |\n"
        "| 中微公司(688012) | 刻蚀设备份额提升，逻辑直接 | 利好 | A级 |\n"
        "| 北方稀土(600111) | 需要确认订单节奏，但稀土涨价传导明确 | 利好 | B级 |\n"
    )

    def test_legit_content_passes(self):
        self.assertIsNotNone(sr._sanitize_stock_picks(self.LEGIT))

    def test_prompt_echo_rejected(self):
        echo = "先梳理资讯：\n（表格至少3-5行）\n好的，我需要先看新闻"
        self.assertIsNone(sr._sanitize_stock_picks(echo))

    def test_no_table_rejected(self):
        self.assertIsNone(sr._sanitize_stock_picks("### 主题\n- 只有一行说明"))

    def test_no_heading_rejected(self):
        self.assertIsNone(sr._sanitize_stock_picks("随便一段没有任何结构化标题的文字"))


class TestDeployOutputs(unittest.TestCase):
    """产出回归：无后缀主文件应为跳转桩，真实内容只写场次文件与 index.html。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mb_test_")
        self._old_base = sr._BASE_DIR
        sr._BASE_DIR = self.tmp
        os.makedirs(os.path.join(self.tmp, "docs"))
        for name in ("report_20260901_am.html", "report_20260901_pm.html",
                     "report_20260901.html", "index.html"):
            with open(os.path.join(self.tmp, "docs", name), "w", encoding="utf-8") as f:
                f.write("<html>legacy</html>")

    def tearDown(self):
        sr._BASE_DIR = self._old_base
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stub_and_archive(self):
        sr.deploy_github_pages("<html>REAL CONTENT</html>", session_slug="pm")
        docs = os.path.join(self.tmp, "docs")
        today = sr.beijing_now().strftime("%Y%m%d")

        with open(os.path.join(docs, f"report_{today}.html"), encoding="utf-8") as f:
            stub = f.read()
        self.assertIn('http-equiv="refresh"', stub)
        self.assertIn(f"report_{today}_pm.html", stub)
        self.assertLess(len(stub), 1200)          # 跳转桩必须极小

        with open(os.path.join(docs, f"report_{today}_pm.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "<html>REAL CONTENT</html>")
        with open(os.path.join(docs, "index.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "<html>REAL CONTENT</html>")

        with open(os.path.join(docs, "archive.html"), encoding="utf-8") as f:
            archive = f.read()
        self.assertNotIn("全天", archive)                       # 跳转桩不单独列行
        self.assertIn("report_20260901_am.html", archive)
        self.assertIn("report_20260901_pm.html", archive)

        # 历史无后缀文件不被覆盖（老链接仍可打开）
        with open(os.path.join(docs, "report_20260901.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "<html>legacy</html>")


SAT = datetime(2026, 9, 12, 22, 17)   # 周六
SUN = datetime(2026, 9, 13, 22, 17)   # 周日
THU = datetime(2026, 9, 10, 22, 17)   # 周四（工作日）
THU_AM = datetime(2026, 9, 10, 7, 17)


class TestSessionFromSchedule(unittest.TestCase):
    """场次判定回归。

    线上现象：22:17 的晚报被 GitHub 调度延迟到次日凌晨才执行，
    旧实现按"执行小时 < 12"判成早报 → 晚报内容写进次日 _am 文件、
    当日 _pm 文件缺失、次日早报又顶掉首页，晚报等于丢失。
    """

    PM_CRON = "17 14 * * *"
    AM_CRON = "17 23 * * *"   # UTC，对应北京时间次日 07:17

    def test_pm_cron_stays_evening_even_after_midnight(self):
        late = datetime(2026, 9, 11, 0, 30)      # 跨过午夜的实际执行时间
        self.assertEqual(sr.get_session_label(late, self.PM_CRON), ("晚报", "pm"))

    def test_am_cron_is_morning(self):
        self.assertEqual(sr.get_session_label(datetime(2026, 9, 11, 7, 17), self.AM_CRON), ("早报", "am"))

    def test_schedule_whitespace_tolerated(self):
        self.assertEqual(sr.get_session_label(None, " 17   14 * * * "), ("晚报", "pm"))

    def test_unknown_schedule_falls_back_to_hour(self):
        self.assertEqual(sr.get_session_label(datetime(2026, 9, 10, 0, 30), "* * * * *"), ("早报", "am"))
        self.assertEqual(sr.get_session_label(datetime(2026, 9, 10, 22, 17), "* * * * *"), ("晚报", "pm"))

    def test_manual_run_without_schedule_uses_hour(self):
        self.assertEqual(sr.get_session_label(datetime(2026, 9, 10, 9, 0), ""), ("早报", "am"))
        self.assertEqual(sr.get_session_label(datetime(2026, 9, 10, 21, 0), ""), ("晚报", "pm"))

    def test_env_schedule_is_used(self):
        old = os.environ.get("GITHUB_EVENT_SCHEDULE")
        os.environ["GITHUB_EVENT_SCHEDULE"] = self.PM_CRON
        try:
            self.assertEqual(sr.get_session_label(datetime(2026, 9, 11, 1, 0)), ("晚报", "pm"))
        finally:
            if old is None:
                os.environ.pop("GITHUB_EVENT_SCHEDULE", None)
            else:
                os.environ["GITHUB_EVENT_SCHEDULE"] = old


class TestReportTime(unittest.TestCase):
    """报告归属时间回归：延迟的晚报必须落在当天 _pm，而不是次日 _am。"""

    def test_late_pm_run_belongs_to_previous_day(self):
        rt = sr.get_report_time("pm", datetime(2026, 9, 11, 0, 30))
        self.assertEqual(rt.strftime("%Y-%m-%d"), "2026-09-10")

    def test_on_time_pm_run_same_day(self):
        rt = sr.get_report_time("pm", datetime(2026, 9, 10, 22, 17))
        self.assertEqual(rt.strftime("%Y-%m-%d"), "2026-09-10")

    def test_morning_run_same_day(self):
        rt = sr.get_report_time("am", datetime(2026, 9, 10, 7, 17))
        self.assertEqual(rt.strftime("%Y-%m-%d"), "2026-09-10")


class TestOpinionDateBase(unittest.TestCase):
    """UP主转录取件日期回归：延迟的晚报应取"当天"而非次日文件。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mb_opinion_")
        name = "9月10日_擒龙先生.12345678.ai-zh.txt"
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write("观点正文" * 20)   # 需 > 50 字，否则被判为过短文件

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _find(tmp, base):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return sr.find_today_opinions(tmp, date_offset=0, base_date=base)

    def test_matches_intended_date(self):
        self.assertEqual(len(self._find(self.tmp, datetime(2026, 9, 10, 23, 30))), 1)

    def test_does_not_match_next_day(self):
        self.assertEqual(len(self._find(self.tmp, datetime(2026, 9, 11, 0, 30))), 0)


class TestSkipRules(unittest.TestCase):
    """休市/周末判断回归。

    背景：曾出现过两个问题——① 早报在开盘前拿不到当天行情，无法自证是否休市，
    节假日仍会出报告；② 若把"行情交易日≠今天"的规则无条件套用，周日晚报会被误杀。
    """

    def test_saturday_always_skipped(self):
        self.assertTrue(sr.get_skip_reason(None, "早报", SAT, set()))
        self.assertTrue(sr.get_skip_reason(None, "晚报", SAT, set()))

    def test_sunday_am_skipped_pm_kept(self):
        self.assertTrue(sr.get_skip_reason(None, "早报", SUN, set()))
        # 周日晚报是"周末版"，必须保留（回归：旧实现会因行情交易日≠今天而误杀）
        self.assertEqual(sr.get_skip_reason([{"trade_date": "2026-09-11"}], "晚报", SUN, set()), "")

    def test_holiday_table_blocks_both_sessions(self):
        holidays = {"2026-10-01"}
        morning = sr.get_skip_reason(None, "早报", datetime(2026, 10, 1, 7, 17), holidays)
        evening = sr.get_skip_reason(None, "晚报", datetime(2026, 10, 1, 22, 17), holidays)
        self.assertIn("法定休市日", morning)
        self.assertIn("法定休市日", evening)

    def test_weekday_evening_stale_trade_date_skipped(self):
        reason = sr.get_skip_reason([{"trade_date": "2026-09-09"}], "晚报", THU, set())
        self.assertIn("休市", reason)

    def test_weekday_evening_fresh_trade_date_ok(self):
        self.assertEqual(sr.get_skip_reason([{"trade_date": "2026-09-10"}], "晚报", THU, set()), "")

    def test_missing_trade_date_does_not_block(self):
        """网络异常导致 trade_date 缺失时不得拦截，否则会漏发正常报告。"""
        self.assertEqual(sr.get_skip_reason([{"trade_date": ""}], "晚报", THU, set()), "")
        self.assertEqual(sr.get_skip_reason([], "晚报", THU, set()), "")

    def test_morning_not_governed_by_trade_date(self):
        """早报开盘前行情天然是上一交易日 → 不得据此拦截。"""
        self.assertEqual(sr.get_skip_reason([{"trade_date": "2026-09-09"}], "早报", THU_AM, set()), "")

    def test_normal_weekday_runs(self):
        self.assertEqual(sr.get_skip_reason([{"trade_date": "2026-09-10"}], "早报", THU_AM, set()), "")


class TestHolidayFile(unittest.TestCase):
    """节假日表读取回归：缺文件/坏文件都不能影响正常出报。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mb_holiday_")
        self._old = sr.MARKET_HOLIDAYS_FILE

    def tearDown(self):
        sr.MARKET_HOLIDAYS_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_returns_empty(self):
        sr.MARKET_HOLIDAYS_FILE = os.path.join(self.tmp, "not_exist.json")
        self.assertEqual(sr.load_market_holidays(), set())

    def test_reads_dates(self):
        path = os.path.join(self.tmp, "holidays.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"holidays": ["2026-10-01", "2026-10-02", ""]}, f)
        sr.MARKET_HOLIDAYS_FILE = path
        self.assertEqual(sr.load_market_holidays(), {"2026-10-01", "2026-10-02"})

    def test_malformed_file_returns_empty(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        sr.MARKET_HOLIDAYS_FILE = path
        self.assertEqual(sr.load_market_holidays(), set())


class TestRunSummary(unittest.TestCase):
    """运行摘要回归：降级状态必须能落到 Actions 运行页，而不是只藏在日志里。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mb_summary_")
        self.summary_path = os.path.join(self.tmp, "summary.md")
        os.environ["GITHUB_STEP_SUMMARY"] = self.summary_path

    def tearDown(self):
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_written_to_step_summary(self):
        rows = [("盘面分析", "降级（未发布草稿）", ""), ("AI选股", "正常", "")]
        sr.write_run_summary(rows)
        with open(self.summary_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("| 项目 | 状态 | 备注 |", content)
        self.assertIn("盘面分析", content)
        self.assertIn("降级（未发布草稿）", content)
        self.assertIn("| AI选股 | 正常 | — |", content)   # 空备注渲染为占位符


class TestSentimentParsing(unittest.TestCase):
    """情绪硬指标解析回归。夹具为 2026-09-10 实测真实响应。"""

    REAL_BREADTH = {
        "rc": 0, "data": {"total": 2, "diff": [
            {"f3": -0.43, "f6": 779672692269.7, "f12": "000001", "f14": "上证指数",
             "f104": 462, "f105": 1841, "f106": 49},
            {"f3": -0.77, "f6": 867475137215.2039, "f12": "399001", "f14": "深证成指",
             "f104": 487, "f105": 2402, "f106": 42},
        ]},
    }
    REAL_ZT = {"rc": 0, "data": {"tc": 35, "qdate": 20260910, "pool": [{"c": "000978"}]}}
    REAL_DT = {"rc": 0, "data": {"tc": 11, "qdate": 20260910, "pool": [{"c": "002365"}]}}

    def test_breadth_aggregates_two_markets(self):
        b = sr.parse_breadth_payload(self.REAL_BREADTH)
        self.assertEqual((b["up"], b["down"], b["flat"]), (949, 4243, 91))
        self.assertAlmostEqual(b["turnover_yi"], 16471.5, places=1)
        self.assertEqual(len(b["indexes"]), 2)
        self.assertEqual(b["indexes"][0][0], "上证指数")

    def test_breadth_bad_payload_returns_none(self):
        self.assertIsNone(sr.parse_breadth_payload(None))
        self.assertIsNone(sr.parse_breadth_payload({"data": None}))
        self.assertIsNone(sr.parse_breadth_payload({"data": {"diff": []}}))

    def test_pool_counts(self):
        self.assertEqual(sr.parse_pool_count(self.REAL_ZT), 35)
        self.assertEqual(sr.parse_pool_count(self.REAL_DT), 11)
        self.assertIsNone(sr.parse_pool_count({"data": None}))
        self.assertIsNone(sr.parse_pool_count(None))

    def test_sector_list(self):
        payload = {"data": {"diff": [
            {"f14": "半导体", "f3": 4.2}, {"f14": "证券", "f3": 3.1},
            {"f14": "煤炭", "f3": -2.5}, {"f14": "银行", "f3": 0.4},
            {"f14": "医药", "f3": -1.8}, {"f14": "钢铁", "f3": 0.1},
        ]}}
        got = sr.parse_sector_list(payload, limit=5)
        self.assertEqual(len(got), 5)
        self.assertEqual(got[0], ("半导体", 4.2))
        self.assertEqual(sr.parse_sector_list({"data": None}), [])

    def test_format_block(self):
        block = sr.format_sentiment_block(
            {"breadth": sr.parse_breadth_payload(self.REAL_BREADTH),
             "limit": {"zt": 35, "dt": 11},
             "sectors": {"top": [("半导体", 4.2)], "bottom": [("煤炭", -2.5)]}},
            trade_date_label="2026-09-10")
        self.assertIn("市场情绪硬指标（2026-09-10）", block)
        self.assertIn("上涨 949 家 / 下跌 4243 家", block)
        self.assertIn("涨停 35 家", block)
        self.assertIn("板块涨幅前五：半导体 +4.20%", block)
        self.assertEqual(sr.format_sentiment_block({}), "")

    def test_partial_data_degrades_gracefully(self):
        """板块接口挂掉时，其余指标照常输出，不出现空行占位或异常。"""
        block = sr.format_sentiment_block(
            {"breadth": sr.parse_breadth_payload(self.REAL_BREADTH)}, trade_date_label="2026-09-10")
        self.assertIn("两市成交额", block)
        self.assertNotIn("板块", block)
        self.assertNotIn("涨跌停", block)


class TestQuotaSelection(unittest.TestCase):
    """来源配额回归。

    旧行为 articles[:30]：A股源顺序 快讯(20)+公告(10)+公司(10)+要闻(10)+新浪(10)，
    前两个源吃满 30 条上限 → 公司新闻/要闻栏目/新浪滚动 三个源每次都被整段丢弃。
    """

    def _articles(self, bucket, n):
        return [{"bucket": bucket, "title": f"{bucket}-{i}"} for i in range(n)]

    def test_all_sources_survive(self):
        arts = (self._articles("em102", 20) + self._articles("em103", 10)
                + self._articles("em110", 10) + self._articles("em_news_list", 10)
                + self._articles("sina_roll", 10))
        picked = sr.select_news_by_quota(arts)
        self.assertEqual(len(picked), 45)
        got = {}
        for a in picked:
            got[a["bucket"]] = got.get(a["bucket"], 0) + 1
        # 每个源都拿到保底配额，不再被前排源挤掉
        self.assertGreaterEqual(got.get("em102", 0), 12)
        self.assertGreaterEqual(got.get("em103", 0), 8)
        self.assertGreaterEqual(got.get("em110", 0), 8)
        self.assertGreaterEqual(got.get("em_news_list", 0), 6)
        self.assertGreaterEqual(got.get("sina_roll", 0), 6)

    def test_small_list_passes_through(self):
        arts = self._articles("em102", 3)
        self.assertEqual(len(sr.select_news_by_quota(arts)), 3)

    def test_cap_enforced_for_single_source(self):
        self.assertEqual(len(sr.select_news_by_quota(self._articles("em102", 60))), 45)

    def test_unknown_bucket_uses_default_quota(self):
        """无 bucket 的老数据按 type 兜底配额；总量够时会截断到上限。"""
        arts = self._articles("em102", 45) + self._articles("快讯", 20)
        picked = sr.select_news_by_quota(arts)
        self.assertEqual(len(picked), 45)
        unknown = [a for a in picked if a["bucket"] == "快讯"]
        self.assertEqual(len(unknown), 6)   # 保底配额生效，不被 em102 挤光


class TestFormatNews(unittest.TestCase):
    """喂给 LLM 的文本回归：情绪指标要进得去，样本不足要标注。"""

    def _news(self, market, bucket, n):
        return [{"title": f"{market}标题{i}", "summary": "", "time": "09-10 10:00",
                 "source": "测试", "market": market, "type": "快讯", "bucket": bucket}
                for i in range(n)]

    def test_sentiment_block_included(self):
        sentiment = {"breadth": sr.parse_breadth_payload(TestSentimentParsing.REAL_BREADTH),
                     "limit": {"zt": 35, "dt": 11}}
        text = sr.format_news(self._news("A股", "em102", 20), sentiment=sentiment,
                              trade_date_label="2026-09-10")
        self.assertIn("市场情绪硬指标（2026-09-10）", text)
        self.assertIn("涨停 35 家", text)

    def test_thin_market_flagged(self):
        text = sr.format_news(self._news("A股", "em102", 20) + self._news("港股", "hk_news", 2))
        self.assertIn("样本不足", text)

    def test_quota_note_when_truncated(self):
        # 条目数超过上限才会出现"精选"提示（不足时全部送入，无需提示）
        text = sr.format_news(self._news("A股", "em102", 60))
        self.assertIn("精选", text)
        self.assertNotIn("样本不足", text)


if __name__ == "__main__":
    unittest.main()
