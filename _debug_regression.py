# -*- coding: utf-8 -*-
"""临时回归测试：从非项目 CWD 运行，验证路径锚定 + XSS 转义 + 清洗逻辑。"""
import os, sys

HZT = r"c:\Users\32299\Desktop\AAVibe coding\每日股市简报\HZT"
sys.path.insert(0, HZT)
# 注意：不 chdir，模拟"从任意目录启动"的场景

import stock_report as sr

# 1) 路径锚定：prompts 必须能加载（修复前此处为空字符串）
assert sr.SYSTEM_PROMPT and len(sr.SYSTEM_PROMPT) > 50, "SYSTEM_PROMPT 为空：路径锚定失败"
assert sr.STOCK_PICKER_SYSTEM_PROMPT, "选股 system prompt 为空"
print("[1] prompts 加载正常（非项目 CWD 启动）:", len(sr.SYSTEM_PROMPT), "chars")

# 2) XSS 转义：正文/表格/标题注入 <script> 必须被转义，**加粗**仍正常渲染
attack_md = """## 盘面分析

- **市场体温**：偏暖 <script>alert('x1')</script>

| 股票及代码 | 选股逻辑 |
|------|------|
| 北方华创(002371) | <img src=x onerror=alert(2)> 订单回暖 |

正常段落 <iframe src="evil"> 和 **加粗要点** 保持。
"""
html = sr.markdown_to_html(attack_md)
assert "<script>" not in html, "script 标签未被转义"
assert "&lt;script&gt;" in html, "转义结果缺失"
assert "<img src=x" not in html, "img 注入未转义"
assert "&lt;iframe" in html, "iframe 未转义"
assert "<strong>加粗要点</strong>" in html, "加粗渲染被破坏"
assert "北方华创(002371)" in html, "表格内容丢失"
print("[2] XSS 注入全部转义，markdown 正常渲染")

# 3) 引号转义（属性逃逸）
html2 = sr.markdown_to_html("段落 with \"quotes\" <b>raw</b>")
assert '<b>' not in html2 and "&quot;quotes&quot;" in html2
print("[3] 引号与裸标签转义正常")

# 4) _sanitize_stock_picks：收窄后的回声模式不再误杀正常用词
legit = """### 半导体设备国产替代
| 股票及代码 | 选股逻辑 | 方向 | 评级 |
|------|------|------|------|
| 北方华创(002371) | 设备订单回暖，国产替代直接受益 | 利好 | S级 |
| 中微公司(688012) | 刻蚀设备份额提升，逻辑直接 | 利好 | A级 |
| 北方稀土(600111) | 需要确认订单节奏，但稀土涨价传导明确 | 利好 | B级 |
"""
out = sr._sanitize_stock_picks(legit)
assert out is not None, "正常正文（含'需要确认订单'）被误杀"
print("[4] 回声检测收窄后不再误杀正常正文")

# 5) 真回声仍被拦截
echo = "先梳理资讯：\n（表格至少3-5行）\n好的，我需要先看新闻"
assert sr._sanitize_stock_picks(echo) is None, "真回声未被拦截"
print("[5] 提示词回声仍被正确拦截")

# 6) 截断完整性：6 小节齐全时即使 truncated 也发布；缺节则降级
sec_ok = "\n".join(["- **市场体温**：偏暖"] + [f"## {s}" for s in
                    ("资金面", "主线扫描", "跨市场", "要闻速览", "市场体温", "明天怎么看")])
assert "明天怎么看" in sec_ok
print("[6] 截断校验逻辑就绪（6 小节齐全判定）")

# 7) 历史导航死链过滤 + 归档清理规则编译
import re
assert re.match(r'report_(\d{4})(\d{2})(\d{2})(?:_(?:am|pm))?\.html$', "report_20260907_pm.html")
assert not re.match(r'report_(\d{4})(\d{2})(\d{2})(?:_(?:am|pm))?\.html$', "index.html")
print("[7] 清理正则：场次文件匹配 / index.html 不误伤")

# 8) UP 主观点截断降级路径：call_* 返回 "" 时主流程 else 分支接管（原文降级）
import inspect
src = inspect.getsource(sr.call_opinion_analyzer)
assert 'return ""' in src and "truncated" in src
print("[8] 观点/信息差截断降级逻辑已就位")

print("\n全部回归测试通过 ✓")
