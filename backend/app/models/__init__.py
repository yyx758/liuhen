"""导入本包即注册全部 ORM 模型；建表、迁移和启动检查依赖完整的 Base.metadata。"""

from backend.app.models import account_event, action, conversation, operation_event, request, task, user  # noqa: F401
