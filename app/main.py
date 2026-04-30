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
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.agent import run_check_cycle
from app.config import config
from app.escalation_settings import save_settings_from_api, settings_api_dict
from app.database import (
    clear_all_thread_data,
    count_escalation_events,
    delete_all_data,
    delete_all_escalation_events,
    delete_escalation_event,
    delete_mailbox,
    delete_support_staff,
    delete_thread,
    get_mailbox_raw,
    get_thread_messages,
    init_db,
    insert_mailbox,
    insert_support_staff,
    list_all_threads,
    list_escalation_events,
    list_mailboxes,
    list_processed_messages,
    list_support_staff,
    update_mailbox,
)
from app.mail_service import fetch_unread_emails, run_mailbox_transport_tests
from app.services.lead_service import (
    list_intent_rows,
    remove_all_intents,
    remove_intent,
)
from app.services.product_service import (
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
        f"   全局兜底收件人(生效): {_esc['effective_default_email'] or '（未配置）'}"
    )
    logger.info("   Dashboard: http://localhost:8000/dashboard")
    if config.AUTO_START_POLLING:
        if _start_polling_background():
            logger.info("   定时轮询：已默认开启（停止需 POST /stop-auto 或进程退出）")
        else:
            logger.info("   定时轮询：已在运行（跳过重复启动）")
    else:
        logger.info("   定时轮询：未自动开启（AUTO_START_POLLING=false）")
    yield
    await _stop_polling_background(log_stop=False)
    logger.info("👋 服务已关闭")


app = FastAPI(
    title="客服邮件工作台",
    description="被动入站客服邮件处理：情绪分析、安抚回复、升级产品负责人",
    version="5.0.0",
    lifespan=lifespan,
)


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
    products = list_product_rows()
    intents = list_intent_rows(limit=200)
    escalations = count_escalation_events()
    processed = list_processed_messages(limit=200)
    staff = list_support_staff()
    return {
        "products": len(products),
        "intents": len(intents),
        "escalations": escalations,
        "processed": len(processed),
        "support_staff": len(staff),
    }


@app.get("/")
async def root():
    return {
        "status": "ok",
        "agent": "客服邮件工作台",
        "version": "5.0.0",
        "description": "被动入站客服：情绪分析 / 安抚回复 / 升级产品负责人",
    }


@app.get("/status")
async def get_status():
    esc = settings_api_dict()
    return {
        "auto_polling": _is_running,
        "poll_interval_seconds": config.POLL_INTERVAL,
        "auto_start_polling_default": config.AUTO_START_POLLING,
        "email_accounts": [x["email_address"] for x in list_mailboxes()],
        "mailbox_count": len(list_mailboxes()),
        "global_brand": config.BRAND_NAME,
        "llm_model": config.LLM_MODEL,
        "default_support_owner": esc["effective_default_email"],
        "summary": _summary(),
    }


@app.get("/settings/escalation")
async def get_escalation_settings_api():
    """全局升级收件：数据库覆盖项 + 生效值 + .env 原始值（只读对照）。"""
    return settings_api_dict()


@app.put("/settings/escalation")
async def put_escalation_settings_api(payload: dict):
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
async def start_auto():
    if _is_running:
        return {"status": "already_running", "poll_interval_seconds": config.POLL_INTERVAL}
    _start_polling_background()
    return {"status": "started", "poll_interval_seconds": config.POLL_INTERVAL}


@app.post("/stop-auto")
async def stop_auto():
    if not _is_running and not _bg_task:
        return {"status": "not_running"}
    await _stop_polling_background(log_stop=True)
    return {"status": "stopped"}


@app.post("/check")
async def check_now():
    if _check_lock.locked():
        raise HTTPException(status_code=409, detail="已有检查任务正在后台运行，请勿重复点击。")
    try:
        async with _check_lock:
            result = await asyncio.to_thread(run_check_cycle)
        return {"status": "success", **result}
    except Exception as exc:
        _raise_server_error(exc)


@app.get("/emails")
async def list_emails(limit: int = 10, mailbox_id: int = 1):
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
async def mailboxes_list():
    return {"mailboxes": list_mailboxes()}


@app.post("/mailboxes")
async def mailboxes_create(payload: dict):
    try:
        row = insert_mailbox(payload)
        return {"status": "ok", "mailbox": row}
    except Exception as exc:
        _raise_bad_request(exc)


@app.put("/mailboxes/{mailbox_id}")
async def mailboxes_update(mailbox_id: int, payload: dict):
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
async def mailboxes_delete(mailbox_id: int):
    if not delete_mailbox(mailbox_id):
        raise HTTPException(status_code=404, detail="mailbox not found")
    return {"status": "deleted", "id": mailbox_id}


@app.post("/mailboxes/{mailbox_id}/test")
async def mailbox_test(mailbox_id: int):
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
async def list_products_api():
    rows = list_product_rows()
    return {"count": len(rows), "products": rows}


@app.post("/products")
async def save_product_api(payload: dict):
    try:
        product = save_product(payload)
    except Exception as exc:
        _raise_bad_request(exc)
    return {"status": "ok", "product": product}


@app.delete("/products/{product_id}")
async def delete_product_api(product_id: str):
    ok = remove_product(product_id)
    if not ok:
        raise HTTPException(status_code=404, detail="产品不存在")
    return {"status": "deleted", "product_id": product_id}


@app.delete("/products")
async def delete_all_products_api():
    count = remove_all_products()
    return {"status": "ok", "deleted_count": count}


# ─── 内部客服处理人员（产品负责人下拉数据源）──────────────────────────────────

@app.get("/support-staff")
async def list_support_staff_api():
    rows = list_support_staff()
    return {"count": len(rows), "staff": rows}


@app.post("/support-staff")
async def add_support_staff_api(payload: dict):
    try:
        row = insert_support_staff(
            display_name=str(payload.get("display_name") or ""),
            email=str(payload.get("email") or ""),
        )
    except ValueError as exc:
        _raise_bad_request(exc)
    return {"status": "ok", "staff": row}


@app.delete("/support-staff/{staff_id}")
async def delete_support_staff_api(staff_id: int):
    if not delete_support_staff(staff_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"status": "deleted", "id": staff_id}


# ─── 意图/客诉分析 ──────────────────────────────────────────────────────────────

@app.get("/intents")
async def list_intents_api(limit: int = 100):
    rows = list_intent_rows(limit=limit)
    return {"count": len(rows), "intents": rows}


@app.delete("/intents/{intent_id}")
async def delete_intent_api(intent_id: int):
    ok = remove_intent(intent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"status": "deleted", "intent_id": intent_id}


@app.delete("/intents")
async def delete_all_intents_api():
    count = remove_all_intents()
    return {"status": "ok", "deleted_count": count}


# ─── 内部通知（escalation）留痕 ─────────────────────────────────────────────────

@app.get("/escalations")
async def list_escalations_api(limit: int = 100):
    rows = list_escalation_events(limit=limit)
    return {"count": len(rows), "escalations": rows}


@app.delete("/escalations/{escalation_id}")
async def delete_escalation_api(escalation_id: int):
    ok = delete_escalation_event(escalation_id)
    if not ok:
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"status": "deleted", "escalation_id": escalation_id}


@app.delete("/escalations")
async def delete_all_escalations_api():
    count = delete_all_escalation_events()
    logger.info(f"🗑️ 已清空内部通知留痕 {count} 条")
    return {"status": "ok", "deleted_count": count}


# ─── 客户会话（技术字段 thread_id）────────────────────────────────────────────

@app.get("/kols")
async def list_kols(mailbox_id: int | None = None):
    threads = list_all_threads(mailbox_id=mailbox_id)
    return {"count": len(threads), "kols": threads, "mailbox_id": mailbox_id}


@app.get("/thread/{thread_id}")
async def get_thread(thread_id: str, limit: int = 30):
    rows = get_thread_messages(thread_id, limit=limit)
    return {"thread_id": thread_id, "count": len(rows), "messages": rows}


@app.delete("/thread/{thread_id}")
async def delete_thread_api(thread_id: str):
    deleted = delete_thread(thread_id)
    return {"status": "deleted", "thread_id": thread_id, "deleted_rows": deleted}


@app.delete("/threads")
async def clear_all_threads_api():
    result = clear_all_thread_data()
    logger.info(f"🗑️ 已清空全部线程数据: {result}")
    return {"status": "ok", **result}


# ─── 已处理邮件 ─────────────────────────────────────────────────────────────────

@app.get("/processed")
async def list_processed(limit: int = 100, mailbox_id: int | None = None):
    records = list_processed_messages(limit=limit, mailbox_id=mailbox_id)
    return {"count": len(records), "records": records}


# ─── 日志 ──────────────────────────────────────────────────────────────────────

@app.get("/logs")
async def get_logs(tail: int = 80):
    logs = list(_log_buffer)[-tail:]
    return {"count": len(logs), "logs": logs}


@app.delete("/logs")
async def clear_logs():
    """清空当前进程内存中的运行日志（便于开发时刷新视图）。"""
    n = len(_log_buffer)
    _log_buffer.clear()
    return {"status": "ok", "cleared_count": n}


# ─── 重置 ──────────────────────────────────────────────────────────────────────

@app.delete("/all-data")
async def delete_all_api():
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
