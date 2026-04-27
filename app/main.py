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
from app.database import (
    clear_all_thread_data,
    count_escalation_events,
    delete_all_data,
    delete_all_escalation_events,
    delete_escalation_event,
    delete_thread,
    get_thread_messages,
    init_db,
    list_all_threads,
    list_escalation_events,
    list_processed_messages,
)
from app.mail_service import fetch_unread_emails
from app.services.creator_service import (
    list_creator_rows,
    patch_creator,
    remove_all_creators,
    remove_creator,
    save_creator,
)
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
_dashboard_path = Path(__file__).parent / "web" / "dashboard.html"
_bg_task = None
_is_running = False


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("🚀 客服邮件工作台启动")
    logger.info(f"   品牌: {config.BRAND_NAME}")
    logger.info(f"   邮箱: {config.EMAIL_ADDRESS}")
    logger.info(f"   LLM:  {config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    logger.info(f"   默认升级负责人: {config.DEFAULT_SUPPORT_OWNER_EMAIL or '（未配置）'}")
    logger.info("   Dashboard: http://localhost:8000/dashboard")
    yield
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
            run_check_cycle()
        except Exception as exc:
            logger.error(f"❌ 轮询异常: {exc}", exc_info=True)
        await asyncio.sleep(config.POLL_INTERVAL)


def _summary() -> dict:
    contacts = list_creator_rows()
    products = list_product_rows()
    intents = list_intent_rows(limit=200)
    escalations = count_escalation_events()
    processed = list_processed_messages(limit=200)
    return {
        "contacts": len(contacts),
        "products": len(products),
        "intents": len(intents),
        "escalations": escalations,
        "processed": len(processed),
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
    return {
        "auto_polling": _is_running,
        "poll_interval_seconds": config.POLL_INTERVAL,
        "email_account": config.EMAIL_ADDRESS,
        "brand": config.BRAND_NAME,
        "llm_model": config.LLM_MODEL,
        "default_support_owner": config.DEFAULT_SUPPORT_OWNER_EMAIL,
        "summary": _summary(),
    }


@app.post("/start-auto")
async def start_auto():
    global _bg_task, _is_running
    if _is_running:
        return {"status": "already_running"}
    _is_running = True
    _bg_task = asyncio.create_task(_polling_loop())
    logger.info(f"🤖 已启动后台轮询，间隔 {config.POLL_INTERVAL} 秒")
    return {"status": "started", "poll_interval_seconds": config.POLL_INTERVAL}


@app.post("/stop-auto")
async def stop_auto():
    global _bg_task, _is_running
    if not _is_running:
        return {"status": "not_running"}
    _is_running = False
    if _bg_task:
        _bg_task.cancel()
        _bg_task = None
    logger.info("⏹️ 已停止后台轮询")
    return {"status": "stopped"}


@app.post("/check")
async def check_now():
    try:
        result = run_check_cycle()
        return {"status": "success", **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/emails")
async def list_emails(limit: int = 10):
    try:
        emails = fetch_unread_emails(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {
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


# ─── 联系人（creators 表，客服语义） ────────────────────────────────────────────

@app.get("/creators")
async def list_creators_api():
    rows = list_creator_rows()
    return {"count": len(rows), "contacts": rows}


@app.post("/creators")
async def save_creator_api(payload: dict):
    try:
        contact = save_creator(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "contact": contact}


@app.put("/creators/{creator_id}")
async def update_creator_api(creator_id: int, payload: dict):
    contact = patch_creator(creator_id, payload)
    if not contact:
        raise HTTPException(status_code=404, detail="联系人不存在")
    return {"status": "ok", "contact": contact}


@app.delete("/creators/{creator_id}")
async def delete_creator_api(creator_id: int):
    ok = remove_creator(creator_id)
    if not ok:
        raise HTTPException(status_code=404, detail="联系人不存在或删除失败")
    return {"status": "ok"}


@app.delete("/creators")
async def delete_all_creators_api():
    count = remove_all_creators()
    return {"status": "ok", "deleted_count": count}


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
        raise HTTPException(status_code=400, detail=str(exc))
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


# ─── 意图/情绪流水 ──────────────────────────────────────────────────────────────

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


# ─── 升级记录 ───────────────────────────────────────────────────────────────────

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
    logger.info(f"🗑️ 已清空升级记录 {count} 条")
    return {"status": "ok", "deleted_count": count}


# ─── 会话/线程 ──────────────────────────────────────────────────────────────────

@app.get("/kols")
async def list_kols():
    threads = list_all_threads()
    return {"count": len(threads), "kols": threads}


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
async def list_processed(limit: int = 100):
    records = list_processed_messages(limit=limit)
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
        raise HTTPException(status_code=500, detail="dashboard 文件不存在")
    return HTMLResponse(_dashboard_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
