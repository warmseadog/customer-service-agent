"""
仪表盘登录：密码哈希、会话签发、启动时 bootstrap 管理员。
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

import bcrypt

from app.config import config
from app.database import (
    auth_count_users,
    auth_create_session,
    auth_create_user,
    auth_delete_session,
    auth_get_session_user,
    auth_get_user_by_username,
)

logger = logging.getLogger(__name__)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8"),
            password_hash.encode("ascii"),
        )
    except (ValueError, TypeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def try_login(username: str, password: str) -> tuple[str, dict[str, Any]] | None:
    """成功返回 (token, public_user)；失败返回 None。"""
    from app.database import auth_purge_expired_sessions

    auth_purge_expired_sessions()
    user = auth_get_user_by_username(username)
    if not user or not int(user.get("is_active") or 0):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    token = new_session_token()
    exp = (datetime.now() + timedelta(days=config.AUTH_SESSION_DAYS)).isoformat()
    auth_create_session(int(user["id"]), token, exp)
    public = {
        "id": int(user["id"]),
        "username": user["username"],
        "role": user["role"],
    }
    return token, public


def try_bootstrap_admin() -> None:
    if auth_count_users() > 0:
        return
    u = (config.AUTH_BOOTSTRAP_ADMIN_USER or "").strip()
    p = (config.AUTH_BOOTSTRAP_ADMIN_PASSWORD or "").strip()
    if not u or not p:
        logger.warning(
            "数据库中尚无用户：请在 .env 设置 AUTH_BOOTSTRAP_ADMIN_USER / "
            "AUTH_BOOTSTRAP_ADMIN_PASSWORD 后重启，或由管理员执行用户创建流程。"
        )
        return
    try:
        auth_create_user(username=u, password_hash=hash_password(p), role="admin")
        logger.info("已根据 AUTH_BOOTSTRAP_* 创建首个管理员: %s", u)
    except Exception as exc:
        logger.error("Bootstrap 管理员失败: %s", exc, exc_info=True)


def user_from_session_token(token: str) -> dict | None:
    """返回用户行（含敏感字段，勿记录日志）。"""
    return auth_get_session_user(token)


def logout_by_token(token: str | None) -> None:
    if token:
        auth_delete_session(token)
