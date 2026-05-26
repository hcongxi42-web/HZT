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
    """配置 matplotlib 中文字体（支持 headless 环境）"""
    import matplotlib
    matplotlib.use("Agg")  # 无 GUI 后端
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 尝试查找系统中文字体
    chinese_fonts = [
        "Noto Sans CJK SC",
        "Noto Sans SC",
        "WenQuanYi Micro Hei",
        "SimHei",
        "Microsoft YaHei",
    ]
    font_found = None
    for font_name in chinese_fonts:
        try:
            font_manager.findfont(font_name, fallback_to_default=False)
            font_found = font_name
            break
        except Exception:
            continue

    if font_found:
        plt.rcParams["font.sans-serif"] = [font_found] + plt.rcParams.get("font.sans-serif", [])
    plt.rcParams["axes.unicode_minus"] = False
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
        fig, axes = plt.subplots(3, 1, figsize=(10, 10), gridspec_kw={"height_ratios": [3, 1, 1]})
        fig.suptitle(f"{stock_name} {stock_code} 技术分析", fontsize=14)

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
    从 LLM 的 Markdown 选股输出中，提取 5-4 星股票列表
    返回: [(name, code, stars, logic), ...]
    """
    if not picks_md:
        return []

    results = []
    lines = picks_md.split("\n")
    for line in lines:
        # 找股票代码：支持 000001.SH、000001、（000001）等格式
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

        # 找股票名称（代码前面的中文）
        name_match = re.search(r"([^|\d\s][^|\d(]*?)\s*[\(（]?\d{6}", line)
        name = name_match.group(1).strip() if name_match else ""

        # 找星级
        stars = line.count("⭐")
        if stars == 0:
            stars = line.count("★")
        if stars >= 4:
            results.append((name, code, stars, ""))

    # 去重
    seen = set()
    unique = []
    for item in results:
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)

    print(f"[StockPicker] 解析到 {len(unique)} 只高评分股票: {[r[1] for r in unique]}")
    return unique[:8]  # 最多分析 8 只


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
