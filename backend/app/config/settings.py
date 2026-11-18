"""应用各模块共用的配置。"""

from datetime import timedelta, timezone

# 当前个人项目使用 UTC+08:00；无时区的任务时间按此解释。
USER_TIMEZONE = timezone(timedelta(hours=8))
USER_TIMEZONE_NAME = "UTC+08:00"
