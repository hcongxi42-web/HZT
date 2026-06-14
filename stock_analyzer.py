"""
股票技术分析模块
- 从 LLM 选股输出中解析股票代码
- 通过 Baostock 获取历史 K 线数据
- 计算技术指标（MA5/10/20, MACD）
- 用 Matplotlib 生成技术分析图
"""

import os
import re
import json
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

# ============ Matplotlib 中文字体配置 ============

def setup_matplotlib():
    """配置 matplotlib 中文字体（支持 headless / GitHub Actions / Windows / Mac）。

    策略：直接扫描文件系统找到 CJK 字体 → 注册 → 强制重建字体列表 → 应用。
    避免依赖 findfont 名称匹配（跨平台不可靠）和字体缓存（往往过时）。
    """
    import matplotlib
    matplotlib.use("Agg")  # 无 GUI 后端
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    import glob as _glob
    import os as _os

    # ---- 1. 清除字体缓存（强制 matplotlib 重建字体列表）----
    cache_dir = matplotlib.get_cachedir()
    try:
        for cf in _glob.glob(_os.path.join(cache_dir, "fontlist*")):
            _os.remove(cf)
        # 删除旧版缓存文件
        for cf in _glob.glob(_os.path.join(cache_dir, "*fontList*")):
            _os.remove(cf)
    except Exception:
        pass

    # ---- 2. 文件系统扫描：找到第一个可用的 CJK 字体文件 ----
    search_roots = [
        "C:/Windows/Fonts",                       # Windows
        "/usr/share/fonts",                        # Linux (apt)
        "/usr/local/share/fonts",                  # Linux (manual)
        _os.path.expanduser("~/.fonts"),           # Linux (user)
        "/System/Library/Fonts",                   # macOS
        "/Library/Fonts",                          # macOS
    ]

    # 优先匹配：非 VF (可变字体) 优先，避免 matplotlib VF 渲染兼容问题
    cjk_keywords_priority = [
        # Windows 首选（.ttc 集合字体，兼容性最好）
        "msyh.ttc", "msyh.ttf",     # 微软雅黑
        "simhei.ttf",                # 黑体
        "simsun.ttc", "simsun.ttf",  # 宋体
        "simkai.ttf",                # 楷体
        # Linux 首选
        "NotoSansCJK", "NotoSansSC",
        "NotoSansMonoCJK",
        "wqy-microhei", "WenQuanYi",
        "DroidSansFallback",
        # macOS 首选
        "PingFang", "Heiti", "STHeiti",
        # 最后的兜底：任何包含 CJK 线索的字体
    ]

    cjk_keywords_broad = [
        "NotoSans", "noto", "CJK",
        "wqy", "WenQuanYi", "wenquan",
        "simhei", "SimHei", "simsun", "SimSun",
        "yahei", "YaHei", "msyh",
        "songti", "heiti", "uming", "ukai",
        "PingFang", "STHeiti",
    ]

    font_path = None
    font_name_from_file = None

    for root_dir in search_roots:
        if not _os.path.isdir(root_dir):
            continue

        # 第一轮：精确匹配（非 VF 优先）
        for dirpath, _dirs, files in _os.walk(root_dir):
            for fn in files:
                if not fn.lower().endswith((".ttf", ".ttc", ".otf")):
                    continue
                fn_lower = fn.lower()
                # 跳过可变字体（Variable Font），matplotlib 对此支持不稳定
                if "vf" in fn_lower and not any(k in fn_lower for k in ["msyh", "simhei", "simsun", "simkai"]):
                    continue
                if not any(kw.lower() in fn_lower for kw in cjk_keywords_priority):
                    continue
                fp = _os.path.join(dirpath, fn)
                try:
                    # 验证该字体确实支持中文
                    from matplotlib.ft2font import FT2Font
                    ft = FT2Font(fp)
                    if ft.get_char_index(ord("中")) > 0:
                        font_path = fp
                        font_name_from_file = ft.family_name
                        break
                except Exception:
                    continue
            if font_path:
                break

        # 第二轮：宽泛匹配（如果第一轮没找到）
        if not font_path:
            for dirpath, _dirs, files in _os.walk(root_dir):
                for fn in files:
                    if not fn.lower().endswith((".ttf", ".ttc", ".otf")):
                        continue
                    fn_lower = fn.lower()
                    if "vf" in fn_lower and "noto" in fn_lower:
                        continue
                    if not any(kw.lower() in fn_lower for kw in cjk_keywords_broad):
                        continue
                    fp = _os.path.join(dirpath, fn)
                    try:
                        from matplotlib.ft2font import FT2Font
                        ft = FT2Font(fp)
                        if ft.get_char_index(ord("中")) > 0:
                            font_path = fp
                            font_name_from_file = ft.family_name
                            break
                    except Exception:
                        continue
                if font_path:
                    break

        if font_path:
            break

    # ---- 3. 注册并应用 ----
    applied = False
    if font_path and font_name_from_file:
        try:
            # 注册字体文件
            font_manager.fontManager.addfont(font_path)
            # 强制重建字体列表
            try:
                font_manager._load_fontmanager(try_read_cache=False)
            except Exception:
                pass

            # 设置全局字体
            plt.rcParams["font.family"] = "sans-serif"
            current_sans = plt.rcParams.get("font.sans-serif", [])
            plt.rcParams["font.sans-serif"] = [font_name_from_file] + [
                f for f in current_sans if f != font_name_from_file
            ]
            plt.rcParams["axes.unicode_minus"] = False
            applied = True
            print(f"[Font] 已注册: {font_name_from_file} ({font_path})")
        except Exception as e:
            print(f"[Font] 注册字体文件失败: {e}")

    # ---- 4. 兜底：按名称设置（仅当文件注册失败时）----
    if not applied:
        fallback_names = [
            "Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
            "Noto Sans SC", "WenQuanYi Micro Hei", "PingFang SC",
        ]
        for name in fallback_names:
            try:
                # 检查字体是否确实存在
                test_path = font_manager.findfont(name, fallback_to_default=False)
                if test_path and _os.path.exists(test_path):
                    plt.rcParams["font.family"] = "sans-serif"
                    current = plt.rcParams.get("font.sans-serif", [])
                    plt.rcParams["font.sans-serif"] = [name] + [f for f in current if f != name]
                    plt.rcParams["axes.unicode_minus"] = False
                    applied = True
                    print(f"[Font] 按名称设置: {name} ({test_path})")
                    break
            except Exception:
                continue

    if not applied:
        print("[Font] 警告：未找到 CJK 字体，中文可能显示为方框")

    return plt


# ============ 股票代码转换 ============

def normalize_stock_code(code_str):
    """
    将各种格式的股票代码转为 Baostock 格式
    输入: 688017.SH, 000001.SZ, 600000 等
    输出: sh.688017, sz.000001
    """
    code_str = code_str.strip().upper()
    # 匹配 6位数字 + .SH/.SZ/.BJ
    m = re.match(r"(\d{6})\.(SH|SZ|BJ)", code_str)
    if m:
        num, market = m.group(1), m.group(2)
        market_lower = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[market]
        return f"{market_lower}.{num}"
    # 匹配纯 6位数字，根据首位判断市场
    m = re.match(r"^(\d{6})$", code_str)
    if m:
        num = m.group(1)
        if num.startswith("6"):
            return f"sh.{num}"
        elif num.startswith("0") or num.startswith("3"):
            return f"sz.{num}"
        elif num.startswith("8") or num.startswith("4"):
            return f"bj.{num}"
    return None


# ============ Baostock 数据获取 ============

def fetch_baostock_data(stock_code, days=60):
    """
    通过 Baostock 获取历史 K 线数据
    返回 DataFrame: date, open, high, low, close, volume, amount
    """
    try:
        import baostock as bs
    except ImportError:
        print(f"[Baostock] 未安装，跳过 {stock_code}")
        return None

    bs_code = normalize_stock_code(stock_code)
    if not bs_code:
        print(f"[Baostock] 无法解析代码: {stock_code}")
        return None

    # 登录（全局只登录一次）
    if not hasattr(fetch_baostock_data, "_logged_in"):
        lg = bs.login()
        if lg.error_code != "0":
            print(f"[Baostock] 登录失败: {lg.error_msg}")
            return None
        fetch_baostock_data._logged_in = True

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")

    fields = "date,open,high,low,close,volume,amount,preclose,pctChg"
    rs = bs.query_history_k_data_plus(bs_code, fields, start_date=start_date, end_date=end_date, frequency="d")

    data_list = []
    while (rs.error_code == "0") and rs.next():
        row = rs.get_row_data()
        data_list.append(row)

    if not data_list:
        print(f"[Baostock] 无数据: {bs_code}")
        return None

    try:
        import pandas as pd
        df = pd.DataFrame(data_list, columns=["date", "open", "high", "low", "close", "volume", "amount", "preclose", "pctChg"])
        numeric_cols = ["open", "high", "low", "close", "volume", "amount", "preclose", "pctChg"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        df = df.dropna()
        # 只取最近 N 天
        df = df.tail(days).reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[Baostock] 数据处理失败: {e}")
        return None


# ============ 技术指标计算 ============

def calculate_indicators(df):
    """计算 MA5/MA10/MA20 和 MACD"""
    if df is None or len(df) < 30:
        return None

    # 均线
    df["MA5"] = df["close"].rolling(window=5).mean()
    df["MA10"] = df["close"].rolling(window=10).mean()
    df["MA20"] = df["close"].rolling(window=20).mean()

    # MACD: EMA12, EMA26, DIF, DEA, MACD柱
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["DIF"] = ema12 - ema26
    df["DEA"] = df["DIF"].ewm(span=9, adjust=False).mean()
    df["MACD_BAR"] = 2 * (df["DIF"] - df["DEA"])

    return df.dropna().reset_index(drop=True)


# ============ 画图 ============

def generate_chart(stock_name, stock_code, df, output_path):
    """
    生成技术分析图：K线+均线 / 成交量 / MACD
    保存为 PNG
    """
    plt = setup_matplotlib()
    df = calculate_indicators(df)
    if df is None or len(df) < 20:
        print(f"[Chart] 数据不足，无法画图: {stock_code}")
        return False

    try:
        fig, axes = plt.subplots(3, 1, figsize=(8, 5.5), gridspec_kw={"height_ratios": [3, 1, 1]})
        fig.suptitle(f"{stock_name} {stock_code}", fontsize=12, fontweight="bold")

        x = range(len(df))
        dates = df["date"].dt.strftime("%m-%d").tolist()
        tick_step = max(1, len(dates) // 6)
        xticks = list(range(0, len(dates), tick_step))
        xticklabels = [dates[i] for i in xticks]

        # ===== 子图1: K线 + 均线 =====
        ax1 = axes[0]
        for i in x:
            o, h, l, c = df["open"][i], df["high"][i], df["low"][i], df["close"][i]
            color = "#e74c3c" if c >= o else "#27ae60"
            ax1.plot([i, i], [l, h], color=color, linewidth=1)
            ax1.plot([i, i], [o, c], color=color, linewidth=3)
        ax1.plot(x, df["MA5"], label="MA5", color="#3498db", linewidth=1)
        ax1.plot(x, df["MA10"], label="MA10", color="#f39c12", linewidth=1)
        ax1.plot(x, df["MA20"], label="MA20", color="#9b59b6", linewidth=1)
        ax1.set_ylabel("价格")
        ax1.legend(loc="upper left")
        ax1.set_xticks(xticks)
        ax1.set_xticklabels([])
        ax1.grid(True, alpha=0.3)

        # ===== 子图2: 成交量 =====
        ax2 = axes[1]
        colors = ["#e74c3c" if df["close"][i] >= df["open"][i] else "#27ae60" for i in x]
        ax2.bar(x, df["volume"], color=colors, width=0.8)
        ax2.set_ylabel("成交量")
        ax2.set_xticks(xticks)
        ax2.set_xticklabels([])
        ax2.grid(True, alpha=0.3)

        # ===== 子图3: MACD =====
        ax3 = axes[2]
        macd_colors = ["#e74c3c" if df["MACD_BAR"][i] >= 0 else "#27ae60" for i in x]
        ax3.bar(x, df["MACD_BAR"], color=macd_colors, width=0.8, label="MACD柱")
        ax3.plot(x, df["DIF"], label="DIF", color="#3498db", linewidth=1)
        ax3.plot(x, df["DEA"], label="DEA", color="#f39c12", linewidth=1)
        ax3.axhline(0, color="gray", linewidth=0.5)
        ax3.set_ylabel("MACD")
        ax3.set_xticks(xticks)
        ax3.set_xticklabels(xticklabels, rotation=45)
        ax3.legend(loc="upper left")
        ax3.grid(True, alpha=0.3)

        plt.tight_layout(rect=[0, 0, 1, 0.96])
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"[Chart] 已生成: {output_path}")
        return True
    except Exception as e:
        print(f"[Chart] 画图失败: {e}")
        return False


# ============ 解析 LLM 选股输出 ============

def parse_stock_picks(picks_md):
    """
    从 LLM 的 Markdown 选股输出中，提取值得画图的股票列表。
    适配新格式：「核心关注」「可以看看」级别 + 表格中的代码 + 把握度列。
    返回: [(name, code, stars, logic), ...]
    """
    if not picks_md:
        return []

    results = []
    lines = picks_md.split("\n")
    current_section_priority = 0  # 0=未进入任何板块, 3=核心关注, 2=可以看看, 1=知道就行

    for line in lines:
        # ── 跟踪 ### 标题，确定当前板块优先级 ──
        stripped = line.strip()
        if stripped.startswith("###") or stripped.startswith("##"):
            if "核心关注" in stripped:
                current_section_priority = 3
            elif "可以看看" in stripped:
                current_section_priority = 2
            elif "知道就行" in stripped or "风险提示" in stripped:
                current_section_priority = 1
            continue

        # ── 找股票代码 ──
        code_match = re.search(r"(\d{6})(?:\.(?:SH|SZ|BJ))?", line, re.IGNORECASE)
        if not code_match:
            continue
        num = code_match.group(1)
        # 根据首位判断市场，添加后缀
        if num.startswith("6"):
            code = f"{num}.SH"
        elif num.startswith("0") or num.startswith("3"):
            code = f"{num}.SZ"
        elif num.startswith("8") or num.startswith("4"):
            code = f"{num}.BJ"
        else:
            continue

        # ── 找股票名称 ──
        name_match = re.search(r"([^|\d\s（）()、，。\n][^|\d（）()\n]*?)\s*[\(（]?\d{6}", line)
        name = name_match.group(1).strip() if name_match else ""

        # ── 确定优先级 ──
        is_table_row = "|" in line
        priority = current_section_priority if current_section_priority > 0 else (2 if is_table_row else 0)

        # ── 从表格「把握度」列提取置信度，调整优先级 ──
        if is_table_row:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 4:
                # 表格列: 股票及代码 | 为什么选它 | 看多/看空 | 把握度
                confidence_col = cells[-1]
                if confidence_col and "高" in confidence_col and "低" not in confidence_col:
                    # 把握度「高」→ 至少值得画图
                    priority = max(priority, 2)
                elif confidence_col and "低" in confidence_col:
                    # 把握度「低」→ 不值得画图
                    priority = min(priority, 1)
            elif cells:
                # 列数较少时退而求其次：任何单元格带「高」都升权
                confidence_col = cells[-1]
                if confidence_col and "高" in confidence_col and "低" not in confidence_col:
                    priority = max(priority, 2)

        # ── 特殊信号：行内显式标注 ──
        if "核心关注" in line:
            priority = 3
        elif "可以看看" in line:
            priority = 2
        elif "知道就行" in line:
            priority = 1

        if priority >= 2:
            results.append((name, code, priority, ""))

    # 按优先级降序排列，去重
    seen = set()
    unique = []
    for item in sorted(results, key=lambda x: -x[2]):
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)

    print(f"[StockPicker] 解析到 {len(unique)} 只股票: {[r[1] for r in unique]}")
    return unique[:15]  # 最多分析 15 只


# ============ 主入口 ============

def analyze_stocks(stock_list, charts_dir, page_base_url):
    """
    对股票列表进行技术分析，生成图表
    stock_list: [(name, code, stars, logic), ...]
    返回: Markdown 图片链接列表
    """
    if not stock_list:
        return []

    today_str = datetime.now().strftime("%Y%m%d")
    chart_urls = []

    for name, code, stars, logic in stock_list:
        df = fetch_baostock_data(code, days=60)
        if df is None:
            continue
        filename = f"{code.replace('.', '_')}_{today_str}.png"
        local_path = os.path.join(charts_dir, filename)
        success = generate_chart(name, code, df, local_path)
        if success and page_base_url:
            url = f"{page_base_url}charts/{filename}"
            chart_urls.append((name, code, stars, url))

    return chart_urls


if __name__ == "__main__":
    # 本地测试
    test_code = "688256.SH"
    df = fetch_baostock_data(test_code, days=60)
    if df is not None:
        generate_chart("寒武纪", test_code, df, "test_chart.png")
