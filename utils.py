"""
共享工具函数
- beijing_now(): 北京时间获取（UTC+8）
"""

from datetime import datetime, timedelta


def beijing_now():
    """返回北京时间 datetime（UTC+8），兼容 CI 和本地环境。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return datetime.utcnow() + timedelta(hours=8)
