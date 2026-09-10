# -*- coding: utf-8 -*-
"""回归测试：把线上踩过的坑固化成断言（纯标准库，CI 无需安装任何依赖）。

运行方式：
    python -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
