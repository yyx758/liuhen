"""应用各模块共用的配置。"""

from datetime import timedelta, timezone

# 当前个人项目使用 UTC+08:00；无时区的任务时间按此解释。
USER_TIMEZONE = timezone(timedelta(hours=8))
USER_TIMEZONE_NAME = "UTC+08:00"

# 文档（PRD D1、D21）：上限由服务层校验，数据库不加长度约束，调整时不需要迁移。
DOCUMENT_MAX_CHARS = 50_000
DOCUMENT_MAX_PER_USER = 500
IMAGE_MAX_BYTES = 5 * 1024 * 1024
IMAGE_MAX_TOTAL_BYTES_PER_USER = 200 * 1024 * 1024

# 切片（spec 1.8）：target 是装箱的软目标，max 是单个片段的硬上限。
CHUNK_TARGET_CHARS = 800
CHUNK_MAX_CHARS = 1_500

# 图片清理（PRD D24）：创建超过这么久、且没有任何本人文档引用的图片会被删除。
IMAGE_ORPHAN_GRACE = timedelta(hours=24)
IMAGE_CLEANUP_INTERVAL_SECONDS = 3600

# 关键词搜索（spec 2.4）
SEARCH_MAX_QUERIES = 5
SEARCH_QUERY_MAX_CHARS = 30
SEARCH_PER_DOCUMENT = 3

# Agent 读文档（spec 3.3）：单次与每轮的正文字数上限，以及每轮最多生成的来源卡片数。
READ_MAX_CHARS_PER_CALL = 6_000
READ_MAX_CHARS_PER_TURN = 24_000
SOURCES_MAX = 5

# Agent 循环上限（PRD D11）：只是防止失控的保险，真正限制检索开销的是阅读量（READ_MAX_CHARS_PER_TURN）。
# 触顶时不报错：不执行多出来的工具调用，再请模型不带工具地收尾（spec 3.5）。
AGENT_MAX_ROUNDS = 15
AGENT_MAX_TOOL_CALLS = 30
# 写入类工具（创建任务、生成提案）单独限次，防止模型批量制造任务或提案。
AGENT_MAX_WRITE_CALLS = 8
