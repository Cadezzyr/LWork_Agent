"""时间工具：按指定 IANA 时区返回当前时间。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool

DEFAULT_TIMEZONE = "Asia/Shanghai"


@tool
def get_current_time(timezone: str = DEFAULT_TIMEZONE) -> str:
    """获取指定时区的当前日期与时间（包含星期与 UTC 偏移）。

    当用户询问"now / 今天 /  текущее 时间 / 今天星期几"等实时信息时应调用本工具，
    不要依赖训练数据中的时间。

    Args:
        timezone: IANA 时区名，例如 "Asia/Shanghai"、"Asia/Tokyo"、"UTC"、"America/New_York"。
    """
    tz_name = (timezone or DEFAULT_TIMEZONE).strip()
    try:
        tzinfo = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        fallback = ZoneInfo("UTC")
        now = datetime.now(fallback)
        return (
            f"未知时区 '{tz_name}'，已回退到 UTC："
            f"{now.strftime('%Y-%m-%d %H:%M:%S %Z')}（{_weekday_cn(now)}）"
        )

    now = datetime.now(tzinfo)
    offset = now.utcoffset()
    offset_hours = 0.0 if offset is None else offset.total_seconds() / 3600
    return (
        f"时区 {tz_name} 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"
        f"（UTC{offset_hours:+g}，{_weekday_cn(now)}）"
    )


def _weekday_cn(moment: datetime) -> str:
    names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return names[moment.weekday()]
