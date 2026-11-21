"""路由共用的依赖：数据库会话与当前用户。"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from backend.app.db.session import get_db
from backend.app.models.user import UserORM

DbSession = Annotated[Session, Depends(get_db)]

# TEMP(P3 接入登录与会话后删除): P2 尚无认证，所有请求都归属这个开发用户。
# 密码哈希 "!" 不是任何算法的合法输出，该账号无法用密码登录。
DEV_USERNAME = "dev"


def current_user_id(session: DbSession) -> int:
    # TEMP(P3 接入登录与会话后删除): 改为从会话 Cookie 解析用户。
    with session.begin():
        session.execute(
            insert(UserORM)
            .values(username=DEV_USERNAME, password_hash="!")
            .on_conflict_do_nothing(index_elements=[UserORM.username])
        )
        return session.scalar(select(UserORM.id).where(UserORM.username == DEV_USERNAME))


# 路由只依赖这个类型；P3 替换 current_user_id 的实现即可，路由不用改。
CurrentUserId = Annotated[int, Depends(current_user_id)]
