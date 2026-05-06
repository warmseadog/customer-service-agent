"""
main.py — FastAPI 入口（客服邮件工作台）
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.agent import run_check_cycle, run_mock_inbound
from app.auth_deps import (
    RequireAdmin,
    RequireAdminOrLead,
    RequireGlobalPolling,
    RequireOperator,
    RequireTeamMailboxScoped,
    RequireViewer,
)
from app.auth_service import (
    hash_password,
    logout_by_token,
    try_bootstrap_admin,
    try_login,
)
from app.rbac_scope import (
    ensure_mailbox_in_scope,
    ensure_thread_in_scope,
    filter_mailboxes_by_scope,
    filter_products_by_scope,
    mailbox_scope,
    summary_for_mailboxes,
)
from app.config import config
from app.escalation_settings import save_settings_from_api, settings_api_dict
from app.database import (
    auth_count_active_admins,
    auth_create_user,
    auth_delete_user,
    auth_get_user_by_id,
    auth_get_user_role,
    auth_list_users,
    auth_purge_expired_sessions,
    auth_update_user,
    bulk_set_manual_handoff_completed,
    clear_all_thread_data,
    count_escalation_events,
    delete_all_data,
    delete_all_escalation_events,
    delete_escalation_event,
    delete_mailbox,
    delete_support_staff,
    delete_thread,
    get_escalation_event,
    get_intent_result,
    get_mailbox_raw,
    get_product,
    get_thread_messages,
    init_db,
    insert_mailbox,
    insert_support_staff,
    list_all_threads,
    list_all_threads_mailboxes,
    list_escalation_events,
    list_escalation_events_for_mailboxes,
    list_intent_results_for_mailboxes,
    list_mailboxes,
    list_processed_messages,
    list_processed_messages_for_mailboxes,
    list_support_staff,
    mock_memory_append_after_run,
    mock_memory_clear,
    mock_memory_coerce_turn,
    mock_memory_get_turns,
    rbac_create_team,
    rbac_delete_team,
    rbac_get_member_mailboxes,
    rbac_lead_can_assign_mailbox,
    rbac_list_teams,
    rbac_replace_member_mailboxes,
    rbac_set_team_mailboxes,
    rbac_update_team,
    sanitize_mock_memory_slot,
    set_thread_manual_handoff,
    update_mailbox,
)
from app.mail_service import fetch_unread_emails, run_mailbox_transport_tests
from app.services.lead_service import (
    list_intent_rows,
    remove_all_intents,
    remove_intent,
)
from app.services.product_service import (
    detach_product_mailbox_link,
    list_product_rows,
    remove_all_products,
    remove_product,
    save_product,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

_log_buffer: deque = deque(maxlen=500)
_web_dir = Path(__file__).parent / "web"
_dashboard_path = _web_dir / "dashboard.html"
_login_path = _web_dir / "login.html"
_bg_task = None
_is_running = False
_check_lock = asyncio.Lock()


class _MemLogHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append(
            {
                "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "message": self.format(record),
            }
        )


_mem_handler = _MemLogHandler()
_mem_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_mem_handler)

_MSG_400 = "请求无效或参数错误，请查看服务日志。"
_MSG_500 = "服务暂时不可用，请稍后再试或查看服务日志。"


def _raise_bad_request(exc: BaseException) -> None:
    logger.warning("HTTP 400: %s", exc, exc_info=True)
    raise HTTPException(status_code=400, detail=_MSG_400) from exc


def _raise_server_error(exc: BaseException) -> None:
    logger.error("HTTP 500: %s", exc, exc_info=True)
    raise HTTPException(status_code=500, detail=_MSG_500) from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    auth_purge_expired_sessions()
    try_bootstrap_admin()
    logger.info("🚀 客服邮件工作台启动")
    mbs = list_mailboxes()
    logger.info(f"   已配置邮箱账户数: {len(mbs)}")
    logger.info(f"   LLM:  {config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    if not (config.LLM_API_KEY or "").strip():
        logger.error(
            "   LLM_API_KEY 为空：所有大模型调用将失败。请检查 .env 是否已保存到磁盘，并重启本进程。"
        )
    _esc = settings_api_dict()
    logger.info(
        "   内部升级通知：仅发往产品负责人(owner)或备用负责人(fallback)；无则不发内部邮件"
    )
    logger.info(
        f"   （仪表盘/.env 兜底邮箱「{_esc['effective_default_email'] or '（未配置）'}」保留配置项，内部路由不使用）"
    )
    logger.info("   Dashboard: http://localhost:8000/dashboard")
    if config.AUTO_START_POLLING:
        if _start_polling_background():
            logger.info("   定时轮询：已默认开启（停止需 POST /stop-auto 或进程退出）")
        else:
            logger.info("   定时轮询：已在运行（跳过重复启动）")
    else:
        logger.info("   定时轮询：未自动开启：请打开仪表盘点击「启动轮询」或 POST /start-auto")
    yield
    await _stop_polling_background(log_stop=False)
    logger.info("👋 服务已关闭")


app = FastAPI(
    title="客服邮件工作台",
    description="被动入站客服邮件处理：情绪分析、安抚回复、升级产品负责人",
    version="5.0.0",
    lifespan=lifespan,
)


def _auth_cookie_response(token: str, body: dict) -> JSONResponse:
    resp = JSONResponse(body)
    resp.set_cookie(
        key=config.AUTH_SESSION_COOKIE,
        value=token,
        max_age=config.AUTH_SESSION_DAYS * 86400,
        httponly=True,
        secure=config.AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/auth/login")
async def auth_login(payload: dict):
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    out = try_login(username, password)
    if not out:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token, user = out
    return _auth_cookie_response(token, {"status": "ok", "user": user})


@app.post("/mock/inbound")
async def mock_inbound_api(payload: dict, user: RequireTeamMailboxScoped):
    """与真实来信相同的分析/话术链路模拟：不落真实会话表、不发邮件。可选读写沙箱专用对话记忆。"""
    raw_mid = payload.get("mailbox_id")
    try:
        mailbox_id = int(raw_mid)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="mailbox_id 须为整数")
    ensure_mailbox_in_scope(user, mailbox_id)
    row = get_mailbox_raw(mailbox_id)
    if not row:
        raise HTTPException(status_code=404, detail="邮箱不存在")
    body_txt = str(payload.get("body") or "")
    if not body_txt.strip():
        raise HTTPException(status_code=400, detail="body 不能为空")
    from_email = str(payload.get("from_email") or "").strip()
    if not from_email or "@" not in from_email:
        raise HTTPException(status_code=400, detail="from_email 须为有效邮箱")
    thread_key_raw = payload.get("thread_key")
    thread_key: str | None
    if thread_key_raw is None or str(thread_key_raw).strip() == "":
        thread_key = None
    else:
        thread_key = str(thread_key_raw).strip()
    product_raw = payload.get("product_id")
    product_id: str | None
    if product_raw is None or str(product_raw).strip() == "":
        product_id = None
    else:
        product_id = str(product_raw).strip()
    prior_raw = payload.get("prior_messages")
    if prior_raw is None:
        client_prior: list = []
    elif isinstance(prior_raw, list):
        client_prior = prior_raw
    else:
        raise HTTPException(status_code=400, detail="prior_messages 须为 JSON 数组")

    use_mem = payload.get("use_conversation_memory")
    if use_mem is None:
        use_mem = True
    persist_mem = payload.get("persist_conversation_memory")
    if persist_mem is None:
        persist_mem = True
    memory_slot = sanitize_mock_memory_slot(str(payload.get("memory_slot") or "default"))

    prior_messages: list = []
    if use_mem:
        prior_messages.extend(mock_memory_get_turns(user.id, mailbox_id, memory_slot))
    for item in client_prior:
        c = mock_memory_coerce_turn(item)
        if c and (c.get("body") or "").strip():
            prior_messages.append(c)

    try:
        result = await asyncio.to_thread(
            run_mock_inbound,
            row,
            from_email=from_email,
            from_name=str(payload.get("from_name") or ""),
            subject=str(payload.get("subject") or ""),
            body=body_txt,
            thread_key=thread_key,
            prior_messages=prior_messages,
            product_id=product_id,
        )
    except Exception as exc:
        _raise_server_error(exc)

    mem_meta = None
    if persist_mem and result.get("dry_run"):
        mem_meta = mock_memory_append_after_run(
            user.id,
            mailbox_id,
            memory_slot,
            customer_body=body_txt.strip(),
            customer_subject=str(payload.get("subject") or ""),
            our_reply=str(result.get("suggested_reply") or ""),
        )
    return {
        "status": "ok",
        **result,
        "conversation_memory_slot": memory_slot,
        "conversation_memory_updated": mem_meta,
        "use_conversation_memory": bool(use_mem),
        "persist_conversation_memory": bool(persist_mem),
    }


@app.get("/mock/memory")
async def mock_memory_read_api(
    user: RequireTeamMailboxScoped,
    mailbox_id: int,
    slot: str = "default",
):
    """读取当前用户对某邮箱的沙箱对话记忆。"""
    ensure_mailbox_in_scope(user, mailbox_id)
    ms = sanitize_mock_memory_slot(slot)
    turns = mock_memory_get_turns(user.id, mailbox_id, ms)
    return {
        "mailbox_id": mailbox_id,
        "slot": ms,
        "turns": turns,
        "count": len(turns),
    }


@app.delete("/mock/memory")
async def mock_memory_delete_api(
    user: RequireTeamMailboxScoped,
    mailbox_id: int,
    slot: str = "default",
):
    """清空本条沙箱对话记忆以便从头演练。"""
    ensure_mailbox_in_scope(user, mailbox_id)
    ms = sanitize_mock_memory_slot(slot)
    deleted = mock_memory_clear(user.id, mailbox_id, ms)
    return {"status": "ok", "mailbox_id": mailbox_id, "slot": ms, "deleted": deleted}


@app.post("/auth/logout")
async def auth_logout(request: Request):
    logout_by_token(request.cookies.get(config.AUTH_SESSION_COOKIE))
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(config.AUTH_SESSION_COOKIE, path="/")
    return resp


@app.get("/auth/me")
async def auth_me(user: RequireViewer):
    mids: list[int] = []
    if user.role == "team_member":
        mids = rbac_get_member_mailboxes(user.id)
    elif user.role == "viewer":
        scoped = mailbox_scope(user)
        if scoped is not None:
            mids = sorted(scoped)
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "mailbox_ids": mids,
    }


def _require_admin_to_assign_admin(actor_role: str, new_role: str) -> None:
    if new_role == "admin" and actor_role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可创建或分配 admin 角色")


def _forbid_non_admin_touching_admin(actor_role: str, target_role: str | None) -> None:
    if actor_role == "admin":
        return
    if (target_role or "") == "admin":
        raise HTTPException(status_code=403, detail="无权管理管理员账号")


@app.get("/auth/users")
async def auth_users_list(_: RequireAdminOrLead):
    rows = auth_list_users()
    out = []
    for u in rows:
        d = dict(u)
        role_s = str(d.get("role") or "")
        if role_s == "team_member" or role_s == "viewer":
            d["mailbox_ids"] = rbac_get_member_mailboxes(int(d["id"]))
        else:
            d["mailbox_ids"] = []
        out.append(d)
    return {"users": out}


@app.post("/auth/users")
async def auth_users_create(actor: RequireAdminOrLead, payload: dict):
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    role = str(payload.get("role") or "viewer").strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="需要 username 与 password")
    _require_admin_to_assign_admin(actor.role, role)
    try:
        row = auth_create_user(
            username=username,
            password_hash=hash_password(password),
            role=role,
        )
    except Exception as exc:
        _raise_bad_request(exc)
    if role in ("team_member", "viewer"):
        mids_raw = payload.get("mailbox_ids")
        if isinstance(mids_raw, list) and mids_raw:
            norm = []
            for x in mids_raw:
                try:
                    norm.append(int(x))
                except (TypeError, ValueError):
                    continue
            if actor.role != "admin":
                for mb in norm:
                    if not rbac_lead_can_assign_mailbox(actor.id, mb):
                        raise HTTPException(status_code=403, detail=f"无权指派邮箱 {mb}")
            rbac_replace_member_mailboxes(int(row["id"]), norm)
    return {"status": "ok", "user": row}


@app.put("/auth/users/{user_id}/mailboxes")
async def auth_user_mailboxes_put(actor: RequireAdminOrLead, user_id: int, payload: dict):
    target = auth_get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    tr = str(target.get("role") or "")
    _forbid_non_admin_touching_admin(actor.role, tr)
    if tr not in ("team_member", "viewer"):
        raise HTTPException(
            status_code=400,
            detail="仅组员或观摩账号可绑定邮箱范围（观摩绑定后仅限只读查阅这些邮箱）",
        )
    mids_raw = payload.get("mailbox_ids")
    if not isinstance(mids_raw, list):
        raise HTTPException(status_code=400, detail="mailbox_ids 须为数组")
    norm = []
    for x in mids_raw:
        try:
            norm.append(int(x))
        except (TypeError, ValueError):
            continue
    if actor.role != "admin":
        for mb in norm:
            if not rbac_lead_can_assign_mailbox(actor.id, mb):
                raise HTTPException(status_code=403, detail=f"无权将邮箱 {mb} 指派给该用户（组长仅能选本组管辖邮箱）")
    rbac_replace_member_mailboxes(user_id, norm)
    return {"status": "ok", "user_id": user_id, "mailbox_ids": rbac_get_member_mailboxes(user_id)}


@app.patch("/auth/users/{user_id}")
async def auth_users_patch(actor: RequireAdminOrLead, user_id: int, payload: dict):
    tgt = auth_get_user_by_id(user_id)
    if not tgt:
        raise HTTPException(status_code=404, detail="用户不存在")
    tgt_role = str(tgt.get("role") or "")
    _forbid_non_admin_touching_admin(actor.role, tgt_role)
    ph = None
    if payload.get("password"):
        ph = hash_password(str(payload["password"]))
    role = payload.get("role")
    if role is not None:
        role = str(role).strip()
        _require_admin_to_assign_admin(actor.role, role)
    ia = payload.get("is_active")
    active: int | None = None if ia is None else (1 if ia else 0)
    if tgt_role == "admin":
        if role is not None and role != "admin":
            if auth_count_active_admins() <= 1 and int(tgt.get("is_active") or 0):
                raise HTTPException(status_code=400, detail="不能降级最后一个管理员")
        if active == 0 and int(tgt.get("is_active") or 0):
            if auth_count_active_admins() <= 1:
                raise HTTPException(status_code=400, detail="不能禁用最后一个管理员")
    try:
        row = auth_update_user(user_id, password_hash=ph, role=role, is_active=active)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"status": "ok", "user": row}


@app.delete("/auth/users/{user_id}")
async def auth_users_delete(actor: RequireAdminOrLead, user_id: int):
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    tgt_role = auth_get_user_role(user_id)
    if not tgt_role:
        raise HTTPException(status_code=404, detail="用户不存在")
    _forbid_non_admin_touching_admin(actor.role, tgt_role)
    if tgt_role == "admin" and auth_count_active_admins() <= 1:
        raise HTTPException(status_code=400, detail="不能删除最后一个管理员")
    if not auth_delete_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"status": "deleted", "id": user_id}


# ─── 小组与邮箱绑定（仅管理员配置组长管辖邮箱）──────────────────────────────────


@app.get("/teams")
async def teams_list(_: RequireAdmin):
    return {"teams": rbac_list_teams()}


@app.post("/teams")
async def teams_create(_: RequireAdmin, payload: dict):
    name = str(payload.get("name") or "小组")
    lead_user_id = int(payload.get("lead_user_id") or 0)
    if lead_user_id <= 0:
        raise HTTPException(status_code=400, detail="需要 lead_user_id")
    row = rbac_create_team(name=name, lead_user_id=lead_user_id)
    return {"status": "ok", "team": row}


@app.patch("/teams/{team_id}")
async def teams_patch(_: RequireAdmin, team_id: int, payload: dict):
    name = payload.get("name")
    lu = payload.get("lead_user_id")
    lead_id = int(lu) if lu is not None else None
    row = rbac_update_team(
        team_id,
        name=str(name) if name is not None else None,
        lead_user_id=lead_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="小组不存在")
    return {"status": "ok", "team": row}


@app.delete("/teams/{team_id}")
async def teams_delete(_: RequireAdmin, team_id: int):
    if not rbac_delete_team(team_id):
        raise HTTPException(status_code=404, detail="小组不存在")
    return {"status": "deleted", "id": team_id}


@app.put("/teams/{team_id}/mailboxes")
async def teams_mailboxes_put(_: RequireAdmin, team_id: int, payload: dict):
    raw = payload.get("mailbox_ids")
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="mailbox_ids 须为数组")
    ids: list[int] = []
    for x in raw:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not any(int(t["id"]) == team_id for t in rbac_list_teams()):
        raise HTTPException(status_code=404, detail="小组不存在")
    rbac_set_team_mailboxes(team_id, ids)
    return {"status": "ok", "team_id": team_id, "mailbox_ids": ids}


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if not _login_path.exists():
        raise HTTPException(status_code=500, detail=_MSG_500)
    return HTMLResponse(_login_path.read_text(encoding="utf-8"))


async def _polling_loop():
    global _is_running
    while _is_running:
        try:
            async with _check_lock:
                await asyncio.to_thread(run_check_cycle)
        except Exception as exc:
            logger.error(f"❌ 轮询异常: {exc}", exc_info=True)
        await asyncio.sleep(config.POLL_INTERVAL)


def _start_polling_background() -> bool:
    """启动后台轮询任务；已在运行时返回 False。"""
    global _bg_task, _is_running
    if _is_running:
        return False
    _is_running = True
    _bg_task = asyncio.create_task(_polling_loop())
    logger.info(f"🤖 已启动后台轮询，间隔 {config.POLL_INTERVAL} 秒")
    return True


async def _stop_polling_background(*, log_stop: bool = True) -> None:
    """取消轮询任务（用于手动停止或服务关闭）。"""
    global _bg_task, _is_running
    if not _bg_task:
        _is_running = False
        return
    _is_running = False
    _bg_task.cancel()
    try:
        await _bg_task
    except asyncio.CancelledError:
        pass
    _bg_task = None
    if log_stop:
        logger.info("⏹️ 已停止后台轮询")


def _summary() -> dict:
    return summary_for_mailboxes(None)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "agent": "客服邮件工作台",
        "version": "5.0.0",
        "description": "被动入站客服：情绪分析 / 安抚回复 / 升级产品负责人",
    }


@app.get("/status")
async def get_status(user: RequireViewer):
    esc = settings_api_dict()
    allowed = mailbox_scope(user)
    mbs = filter_mailboxes_by_scope(list_mailboxes(), allowed)
    return {
        "auto_polling": _is_running,
        "poll_interval_seconds": config.POLL_INTERVAL,
        "auto_start_polling_default": config.AUTO_START_POLLING,
        "email_accounts": [x["email_address"] for x in mbs],
        "mailbox_count": len(mbs),
        "global_brand": config.BRAND_NAME,
        "llm_model": config.LLM_MODEL,
        "default_support_owner": esc["effective_default_email"],
        "summary": summary_for_mailboxes(allowed),
        "role": user.role,
    }


@app.get("/settings/escalation")
async def get_escalation_settings_api(user: RequireViewer):
    """全局升级收件：数据库覆盖项 + 生效值 + .env 原始值（只读对照）。"""
    return settings_api_dict()


@app.put("/settings/escalation")
async def put_escalation_settings_api(payload: dict, user: RequireOperator):
    """保存到数据库；某字段传空字符串则清除覆盖，该字段回退 .env。"""
    try:
        saved = save_settings_from_api(
            default_owner_email=payload.get("default_owner_email"),
            default_owner_name=payload.get("default_owner_name"),
        )
    except Exception as exc:
        _raise_bad_request(exc)
    return {"status": "ok", **saved}


@app.post("/start-auto")
async def start_auto(user: RequireGlobalPolling):
    if _is_running:
        return {"status": "already_running", "poll_interval_seconds": config.POLL_INTERVAL}
    _start_polling_background()
    return {"status": "started", "poll_interval_seconds": config.POLL_INTERVAL}


@app.post("/stop-auto")
async def stop_auto(user: RequireGlobalPolling):
    if not _is_running and not _bg_task:
        return {"status": "not_running"}
    await _stop_polling_background(log_stop=True)
    return {"status": "stopped"}


@app.put("/mailboxes/{mailbox_id}/poll")
async def mailbox_poll_put(mailbox_id: int, payload: dict, user: RequireTeamMailboxScoped):
    """开启/暂停本邮箱参与自动轮询（与全局轮询独立；专员/管理员不限邮箱，组长组员仅限权限内邮箱）。"""
    ensure_mailbox_in_scope(user, mailbox_id)
    if "poll_enabled" not in payload or not isinstance(payload.get("poll_enabled"), bool):
        raise HTTPException(status_code=400, detail="请求体须包含布尔字段 poll_enabled")
    row = update_mailbox(mailbox_id, {"poll_enabled": payload["poll_enabled"]})
    if not row:
        raise HTTPException(status_code=404, detail="邮箱不存在")
    return {"status": "ok", "mailbox": row}


@app.post("/check")
async def check_now(user: RequireOperator):
    if _check_lock.locked():
        raise HTTPException(status_code=409, detail="已有检查任务正在后台运行，请勿重复点击。")
    try:
        async with _check_lock:
            result = await asyncio.to_thread(run_check_cycle)
        return {"status": "success", **result}
    except Exception as exc:
        _raise_server_error(exc)


@app.get("/emails")
async def list_emails(user: RequireViewer, limit: int = 10, mailbox_id: int = 1):
    ensure_mailbox_in_scope(user, mailbox_id)
    try:
        row = get_mailbox_raw(mailbox_id)
        if not row:
            raise HTTPException(status_code=404, detail="mailbox not found")
        emails = fetch_unread_emails(row, limit=limit)
    except HTTPException:
        raise
    except Exception as exc:
        _raise_server_error(exc)
    return {
        "mailbox_id": mailbox_id,
        "count": len(emails),
        "emails": [
            {
                "uid": item["uid"],
                "from": item["from_raw"],
                "subject": item["subject"],
                "date": item["date"],
                "has_message_id": bool(item["message_id"]),
            }
            for item in emails
        ],
    }



# ─── 邮箱账户 ───────────────────────────────────────────────────────────────────

@app.get("/mailboxes")
async def mailboxes_list(user: RequireViewer):
    rows = list_mailboxes()
    return {"mailboxes": filter_mailboxes_by_scope(rows, mailbox_scope(user))}


@app.post("/mailboxes")
async def mailboxes_create(payload: dict, user: RequireOperator):
    try:
        row = insert_mailbox(payload)
        return {"status": "ok", "mailbox": row}
    except Exception as exc:
        _raise_bad_request(exc)


@app.put("/mailboxes/{mailbox_id}")
async def mailboxes_update(mailbox_id: int, payload: dict, user: RequireOperator):
    ensure_mailbox_in_scope(user, mailbox_id)
    try:
        row = update_mailbox(mailbox_id, payload)
        if not row:
            raise HTTPException(status_code=404, detail="mailbox not found")
        return {"status": "ok", "mailbox": row}
    except HTTPException:
        raise
    except Exception as exc:
        _raise_bad_request(exc)


@app.delete("/mailboxes/{mailbox_id}")
async def mailboxes_delete(mailbox_id: int, user: RequireOperator):
    ensure_mailbox_in_scope(user, mailbox_id)
    if not delete_mailbox(mailbox_id):
        raise HTTPException(status_code=404, detail="mailbox not found")
    return {"status": "deleted", "id": mailbox_id}


@app.post("/mailboxes/{mailbox_id}/test")
async def mailbox_test(mailbox_id: int, user: RequireOperator):
    ensure_mailbox_in_scope(user, mailbox_id)
    row = get_mailbox_raw(mailbox_id)
    if not row:
        raise HTTPException(status_code=404, detail="mailbox not found")
    result = run_mailbox_transport_tests(row)
    imap_ok = bool(result["imap"]["ok"])
    smtp_ok = bool(result["smtp"]["ok"])
    if imap_ok and smtp_ok:
        overall = "ok"
    elif imap_ok or smtp_ok:
        overall = "partial"
    else:
        overall = "failed"
    peek = result["imap"]["peek_count"] if imap_ok else 0
    return {
        "status": overall,
        "mailbox_id": mailbox_id,
        "imap": result["imap"],
        "smtp": result["smtp"],
        "peek_count": int(peek or 0),
    }


# ─── 产品库（含 owner 字段，1A 唯一维护入口） ───────────────────────────────────

@app.get("/products")
async def list_products_api(user: RequireViewer):
    rows = list_product_rows()
    rows = filter_products_by_scope(rows, mailbox_scope(user))
    return {"count": len(rows), "products": rows}


@app.post("/products")
async def save_product_api(payload: dict, user: RequireOperator):
    try:
        product = save_product(payload)
    except Exception as exc:
        _raise_bad_request(exc)
    return {"status": "ok", "product": product}


@app.delete("/products/{product_id}")
async def delete_product_api(product_id: str, user: RequireOperator):
    ok = remove_product(product_id)
    if not ok:
        raise HTTPException(status_code=404, detail="产品不存在")
    return {"status": "deleted", "product_id": product_id}


@app.delete("/products/{product_id}/mailboxes/{mailbox_id}")
async def detach_product_mailbox_api(product_id: str, mailbox_id: int, user: RequireOperator):
    """仅从指定收件箱移除产品关联，不删除产品主记录。"""
    ensure_mailbox_in_scope(user, mailbox_id)
    if not get_product(product_id):
        raise HTTPException(status_code=404, detail="产品不存在")
    if not detach_product_mailbox_link(product_id, mailbox_id):
        raise HTTPException(status_code=404, detail="该邮箱未关联此产品")
    return {"status": "ok", "product_id": product_id, "mailbox_id": mailbox_id}


@app.delete("/products")
async def delete_all_products_api(user: RequireAdmin):
    count = remove_all_products()
    return {"status": "ok", "deleted_count": count}


# ─── 内部客服处理人员（产品负责人下拉数据源）──────────────────────────────────

@app.get("/support-staff")
async def list_support_staff_api(user: RequireViewer):
    rows = list_support_staff()
    return {"count": len(rows), "staff": rows}


@app.post("/support-staff")
async def add_support_staff_api(payload: dict, user: RequireOperator):
    try:
        row = insert_support_staff(
            display_name=str(payload.get("display_name") or ""),
            email=str(payload.get("email") or ""),
        )
    except ValueError as exc:
        _raise_bad_request(exc)
    return {"status": "ok", "staff": row}


@app.delete("/support-staff/{staff_id}")
async def delete_support_staff_api(staff_id: int, user: RequireOperator):
    if not delete_support_staff(staff_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"status": "deleted", "id": staff_id}


# ─── 意图/客诉分析 ──────────────────────────────────────────────────────────────

@app.get("/intents")
async def list_intents_api(user: RequireViewer, limit: int = 100):
    allowed = mailbox_scope(user)
    if allowed is None:
        rows = list_intent_rows(limit=limit)
    else:
        mids = sorted(allowed)
        rows = list_intent_results_for_mailboxes(limit, mids) if mids else []
    return {"count": len(rows), "intents": rows}


@app.delete("/intents/{intent_id}")
async def delete_intent_api(intent_id: int, user: RequireOperator):
    row = get_intent_result(intent_id)
    if row:
        ensure_thread_in_scope(user, str(row["thread_id"]))
    ok = remove_intent(intent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"status": "deleted", "intent_id": intent_id}


@app.delete("/intents")
async def delete_all_intents_api(user: RequireAdmin):
    count = remove_all_intents()
    return {"status": "ok", "deleted_count": count}


# ─── 内部通知（escalation）留痕 ─────────────────────────────────────────────────

@app.get("/escalations")
async def list_escalations_api(user: RequireViewer, limit: int = 100):
    allowed = mailbox_scope(user)
    if allowed is None:
        rows = list_escalation_events(limit=limit)
    else:
        mids = sorted(allowed)
        rows = list_escalation_events_for_mailboxes(limit, mids) if mids else []
    return {"count": len(rows), "escalations": rows}


@app.delete("/escalations/{escalation_id}")
async def delete_escalation_api(escalation_id: int, user: RequireOperator):
    row = get_escalation_event(escalation_id)
    if row:
        ensure_thread_in_scope(user, str(row["thread_id"]))
    ok = delete_escalation_event(escalation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"status": "deleted", "escalation_id": escalation_id}


@app.delete("/escalations")
async def delete_all_escalations_api(user: RequireAdmin):
    count = delete_all_escalation_events()
    logger.info(f"🗑️ 已清空内部通知留痕 {count} 条")
    return {"status": "ok", "deleted_count": count}


# ─── 客户会话（技术字段 thread_id）────────────────────────────────────────────

@app.get("/kols")
async def list_kols(user: RequireViewer, mailbox_id: int | None = None):
    allowed = mailbox_scope(user)
    if allowed is None:
        threads = list_all_threads(mailbox_id=mailbox_id)
    else:
        mids = sorted(allowed)
        if not mids:
            threads = []
        elif mailbox_id is not None:
            ensure_mailbox_in_scope(user, mailbox_id)
            threads = list_all_threads(mailbox_id=mailbox_id)
        else:
            threads = list_all_threads_mailboxes(mids)
    return {"count": len(threads), "kols": threads, "mailbox_id": mailbox_id}


@app.get("/thread/{thread_id}")
async def get_thread(user: RequireViewer, thread_id: str, limit: int = 30):
    ensure_thread_in_scope(user, thread_id)
    rows = get_thread_messages(thread_id, limit=limit)
    return {"thread_id": thread_id, "count": len(rows), "messages": rows}


@app.patch("/thread/{thread_id}/manual-handoff")
async def thread_manual_handoff_api(thread_id: str, payload: dict, user: RequireTeamMailboxScoped):
    """组长/组员/管理员：标记人工已结案，后续来信不自动回复（仍归档）。"""
    ensure_thread_in_scope(user, thread_id)
    mc = payload.get("manual_handoff_completed")
    if not isinstance(mc, bool):
        raise HTTPException(status_code=400, detail="请求体须包含布尔字段 manual_handoff_completed")
    row = set_thread_manual_handoff(
        thread_id,
        mc,
        actor_user_id=user.id if mc else None,
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="暂无该会话状态，请待系统处理过一封信件后再标记",
        )
    return {"status": "ok", "thread": row}


@app.post("/threads/bulk-manual-handoff")
async def bulk_thread_manual_handoff_api(payload: dict, user: RequireTeamMailboxScoped):
    """
    将当前权限范围内、与「客户会话」列表一致筛选下的未结案会话，全部标记为人工处理完毕。
    body.mailbox_id：与 GET /kols?mailbox_id= 相同；省略或 null 表示当前角色可见的全部邮箱。
    """
    raw_mb = payload.get("mailbox_id")
    mailbox_id: int | None
    if raw_mb is None or raw_mb == "":
        mailbox_id = None
    else:
        try:
            mailbox_id = int(raw_mb)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="mailbox_id 须为整数或省略")
        ensure_mailbox_in_scope(user, mailbox_id)
    allowed = mailbox_scope(user)
    n = bulk_set_manual_handoff_completed(
        mailbox_id=mailbox_id,
        allowed_mailbox_ids=allowed,
        actor_user_id=user.id,
    )
    return {"status": "ok", "updated_count": n}


@app.delete("/thread/{thread_id}")
async def delete_thread_api(thread_id: str, user: RequireOperator):
    ensure_thread_in_scope(user, thread_id)
    deleted = delete_thread(thread_id)
    return {"status": "deleted", "thread_id": thread_id, "deleted_rows": deleted}


@app.delete("/threads")
async def clear_all_threads_api(user: RequireAdmin):
    result = clear_all_thread_data()
    logger.info(f"🗑️ 已清空全部线程数据: {result}")
    return {"status": "ok", **result}


# ─── 已处理邮件 ─────────────────────────────────────────────────────────────────

@app.get("/processed")
async def list_processed(user: RequireViewer, limit: int = 100, mailbox_id: int | None = None):
    allowed = mailbox_scope(user)
    if allowed is None:
        records = list_processed_messages(limit=limit, mailbox_id=mailbox_id)
    else:
        mids = sorted(allowed)
        if mailbox_id is not None:
            ensure_mailbox_in_scope(user, mailbox_id)
            records = list_processed_messages(limit=limit, mailbox_id=mailbox_id)
        else:
            records = list_processed_messages_for_mailboxes(limit, mids) if mids else []
    return {"count": len(records), "records": records}


# ─── 日志 ──────────────────────────────────────────────────────────────────────

@app.get("/logs")
async def get_logs(user: RequireViewer, tail: int = 80):
    logs = list(_log_buffer)[-tail:]
    return {"count": len(logs), "logs": logs}


@app.delete("/logs")
async def clear_logs(user: RequireOperator):
    """清空当前进程内存中的运行日志（便于开发时刷新视图）。"""
    n = len(_log_buffer)
    _log_buffer.clear()
    return {"status": "ok", "cleared_count": n}


# ─── 重置 ──────────────────────────────────────────────────────────────────────

@app.delete("/all-data")
async def delete_all_api(user: RequireAdmin):
    result = delete_all_data()
    logger.warning(f"🗑️ 已清空全部业务数据: {result}")
    return {"status": "cleared", **result}


# ─── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    if not _dashboard_path.exists():
        logger.error("dashboard 文件不存在: %s", _dashboard_path)
        raise HTTPException(status_code=500, detail=_MSG_500)
    return HTMLResponse(_dashboard_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
