#!/usr/bin/env python3
"""
同步股市日报 PDF 到本地 ~/StockReports/ 文件夹
- 手动运行: python3 sync_reports.py
- 自动运行: 配合 macOS LaunchAgent 定时执行
"""
import json
import os
import urllib.parse
import urllib.request

LOCAL_DIR = os.path.expanduser("~/StockReports")
REPO = "IvyXiashengjie/stock-daily-report"
API_URL = f"https://api.github.com/repos/{REPO}/contents/docs/pdf"


def sync():
    os.makedirs(LOCAL_DIR, exist_ok=True)

    # 获取远程 PDF 列表
    try:
        req = urllib.request.Request(API_URL, headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "StockReportSync/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            files = json.loads(resp.read().decode())
    except Exception as e:
        print(f"获取文件列表失败: {e}")
        return

    if not isinstance(files, list):
        print(f"远程暂无 PDF 文件")
        return

    # 下载新文件
    new_count = 0
    for f in files:
        name = f.get("name", "")
        if not name.endswith(".pdf") or name == "latest.pdf":
            continue
        local_path = os.path.join(LOCAL_DIR, name)
        if os.path.exists(local_path):
            continue
        download_url = f.get("download_url", "")
        if not download_url:
            continue
        # 对中文文件名进行 URL 编码
        parts = download_url.rsplit("/", 1)
        if len(parts) == 2:
            download_url = parts[0] + "/" + urllib.parse.quote(parts[1])
        try:
            print(f"下载: {name} ...", end=" ")
            req = urllib.request.Request(download_url, headers={"User-Agent": "StockReportSync/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                with open(local_path, "wb") as out:
                    out.write(resp.read())
            print("OK")
            new_count += 1
        except Exception as e:
            print(f"失败 ({e})")

    existing = [f for f in os.listdir(LOCAL_DIR) if f.endswith(".pdf")]
    print(f"\n同步完成: 新增 {new_count} 份, 本地共 {len(existing)} 份报告")
    print(f"本地目录: {LOCAL_DIR}")

    if existing:
        existing.sort(reverse=True)
        print(f"最新报告: {existing[0]}")


if __name__ == "__main__":
    sync()
