"""
定时触发 Dify Workflow 并将结果推送到指定渠道
使用前需配置下方的 DIFY_API_KEY 和 WORKFLOW_ID
"""

import json
import urllib.request
import urllib.error
from datetime import datetime

# ============ 配置区域 ============
DIFY_BASE_URL = "http://localhost/v1"  # Dify API 地址
DIFY_API_KEY = "app-yP1tqUN944LsefgRPXAWb5C3"

# 可选：企业微信/钉钉/飞书 Webhook（留空则只打印到终端）
WEBHOOK_URL = ""
# ==================================


def trigger_workflow():
    """触发 Dify Workflow 并获取结果"""
    url = f"{DIFY_BASE_URL}/workflows/run"
    payload = json.dumps({
        "inputs": {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "market": "all",
        },
        "response_mode": "blocking",
        "user": "cron-stock-agent",
    }).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
        return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}: {error_body}"}
    except Exception as e:
        return {"error": str(e)}


def send_webhook(text):
    """推送到企业微信/钉钉/飞书 Webhook"""
    if not WEBHOOK_URL:
        return

    # 企业微信格式
    if "qyapi.weixin" in WEBHOOK_URL:
        payload = json.dumps({
            "msgtype": "markdown",
            "markdown": {"content": text},
        })
    # 钉钉格式
    elif "dingtalk" in WEBHOOK_URL:
        payload = json.dumps({
            "msgtype": "markdown",
            "markdown": {"title": "股市日报", "text": text},
        })
    # 飞书格式
    elif "feishu" in WEBHOOK_URL or "larksuite" in WEBHOOK_URL:
        payload = json.dumps({
            "msg_type": "text",
            "content": {"text": text},
        })
    else:
        payload = json.dumps({"text": text})

    req = urllib.request.Request(
        WEBHOOK_URL,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"Webhook 推送成功: {resp.status}")
    except Exception as e:
        print(f"Webhook 推送失败: {e}")


def main():
    print(f"[{datetime.now()}] 开始触发股市新闻 Workflow...")
    result = trigger_workflow()

    if "error" in result:
        print(f"触发失败: {result['error']}")
        return

    # 提取 Workflow 输出
    outputs = result.get("data", {}).get("outputs", {})
    report = outputs.get("report", outputs.get("text", json.dumps(outputs, ensure_ascii=False)))

    print("=" * 60)
    print(report)
    print("=" * 60)

    # 推送到 Webhook
    send_webhook(report)
    print(f"[{datetime.now()}] 完成")


if __name__ == "__main__":
    main()
