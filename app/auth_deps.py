"""
FastAPI 依赖：Cookie 会话解析与 RBAC。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.config import config
from app.auth_service import user_from_session_token

_MSG_401 = "未登录或会话已失效，请重新登录。"
_MSG_403 = "当前账号权限不足。"


class CurrentUser(dict):
    """{'id', 'username', 'role'}"""

    @property
    def id(self) -> int:
        return int(self["id"])

    @property
    def username(self) -> str:
        return str(self["username"])

    @property
    def role(self) -> str:
        return str(self["role"])


async def get_current_user(request: Request) -> CurrentUser:
    token = request.cookies.get(config.AUTH_SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail=_MSG_401)
    row = user_from_session_token(token)
    if not row:
        raise HTTPException(status_code=401, detail=_MSG_401)
    return CurrentUser(
        id=int(row["id"]),
        username=row["username"],
        role=row["role"],
    )


def require_roles(*roles: str):
    allowed = frozenset(roles)

    async def _dep(user: Annotated[CurrentUser, Depends(get_current_user)]) -> CurrentUser:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail=_MSG_403)
        return user

    return _dep


RequireViewer = Annotated[CurrentUser, Depends(get_current_user)]
RequireOperator = Annotated[
    CurrentUser,
    Depends(require_roles("admin", "operator", "team_lead")),
]
RequireGlobalPolling = Annotated[
    CurrentUser,
    Depends(require_roles("admin", "operator")),
]
RequireTeamMailboxScoped = Annotated[
    CurrentUser,
    Depends(require_roles("admin", "operator", "team_lead", "team_member")),
]
RequireAdmin = Annotated[CurrentUser, Depends(require_roles("admin"))]
RequireAdminOrLead = Annotated[
    CurrentUser,
    Depends(require_roles("admin", "operator", "team_lead")),
]
