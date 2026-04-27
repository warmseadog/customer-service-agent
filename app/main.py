"""
main.py — FastAPI 入口 + 主动达人开发工作台
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
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.agent import run_check_cycle
from app.config import config
from app.database import (
    clear_all_thread_data,
    count_pending_tickets,
    delete_all_data,
    delete_thread,
    get_thread_messages,
    init_db,
    list_all_threads,
    list_processed_messages,
)
from app.mail_service import fetch_unread_emails
from app.services.campaign_service import (
    create_campaign_drafts,
    list_campaign_outreach,
    list_campaign_rows,
    remove_campaign,
    remove_outreach_message,
    send_campaign,
    send_outreach_by_id,
    update_outreach_draft,
)
from app.services.creator_service import (
    import_creators_from_csv_text,
    list_creator_rows,
    patch_creator,
    remove_all_creators,
    remove_creator,
    save_creator,
)
from app.services.lead_service import (
    export_leads_csv,
    list_intent_rows,
    list_lead_rows,
    patch_ticket_status,
    remove_all_intents,
    remove_all_leads,
    remove_intent,
    remove_lead,
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
    logger.info("🚀 Creator Outreach Workbench 启动")
    logger.info(f"   品牌: {config.BRAND_NAME}")
    logger.info(f"   邮箱: {config.EMAIL_ADDRESS}")
    logger.info(f"   LLM:  {config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    logger.info("   Dashboard: http://localhost:8000/dashboard")
    yield
    logger.info("👋 服务已关闭")


app = FastAPI(
    title="Creator Outreach Workbench",
    description="达人主动开发、本地 CRM、批量外呼与意图识别工作台",
    version="4.0.0",
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
    creators = list_creator_rows()
    products = list_product_rows()
    campaigns = list_campaign_rows()
    intents = list_intent_rows(limit=200)
    processed = list_processed_messages(limit=200)
    return {
        "creators": len(creators),
        "products": len(products),
        "campaigns": len(campaigns),
        "leads": count_pending_tickets(),
        "intents": len(intents),
        "processed": len(processed),
    }


@app.get("/")
async def root():
    return {"status": "ok", "agent": "Creator Outreach Workbench", "version": "4.0.0"}


@app.get("/status")
async def get_status():
    return {
        "auto_polling": _is_running,
        "poll_interval_seconds": config.POLL_INTERVAL,
        "email_account": config.EMAIL_ADDRESS,
        "brand": config.BRAND_NAME,
        "llm_model": config.LLM_MODEL,
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


@app.get("/creators")
async def list_creators_api():
    rows = list_creator_rows()
    return {"count": len(rows), "creators": rows}


@app.post("/creators")
async def save_creator_api(payload: dict):
    try:
        creator = save_creator(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "creator": creator}


@app.put("/creators/{creator_id}")
async def update_creator_api(creator_id: int, payload: dict):
    creator = patch_creator(creator_id, payload)
    if not creator:
        raise HTTPException(status_code=404, detail="达人不存在")
    return {"status": "ok", "creator": creator}


@app.delete("/creators/{creator_id}")
async def delete_creator_api(creator_id: int):
    ok = remove_creator(creator_id)
    if not ok:
        raise HTTPException(status_code=404, detail="达人不存在或删除失败")
    return {"status": "ok"}


@app.delete("/creators")
async def delete_all_creators_api():
    count = remove_all_creators()
    return {"status": "ok", "deleted_count": count}


@app.post("/creators/import-csv")
async def import_creators_api(payload: dict):
    try:
        result = import_creators_from_csv_text(payload.get("csv_text", ""))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info(f"📥 达人 CSV 导入完成: {result}")
    return {"status": "ok", **result}


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


@app.get("/campaigns")
async def list_campaigns_api():
    campaigns = list_campaign_rows()
    return {"count": len(campaigns), "campaigns": campaigns}


@app.post("/campaigns/draft")
async def create_campaign_draft_api(payload: dict):
    try:
        result = create_campaign_drafts(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("🧩 已生成批次草稿")
    return {"status": "ok", **result}


@app.delete("/campaigns/{campaign_id}")
async def delete_campaign_api(campaign_id: int):
    ok = remove_campaign(campaign_id)
    if not ok:
        raise HTTPException(status_code=404, detail="批次不存在或删除失败")
    return {"status": "ok"}


@app.get("/campaigns/{campaign_id}/messages")
async def campaign_messages_api(campaign_id: int):
    rows = list_campaign_outreach(campaign_id)
    return {"count": len(rows), "messages": rows}


@app.put("/outreach/{outreach_id}")
async def update_outreach_api(outreach_id: int, payload: dict):
    updated = update_outreach_draft(outreach_id, payload)
    if not updated:
        raise HTTPException(status_code=404, detail="外呼记录不存在")
    return {"status": "ok", "message": updated}


@app.delete("/outreach/{outreach_id}")
async def delete_outreach_api(outreach_id: int):
    ok = remove_outreach_message(outreach_id)
    if not ok:
        raise HTTPException(status_code=404, detail="外呼草稿不存在或删除失败")
    return {"status": "ok"}


@app.post("/outreach/{outreach_id}/send")
async def send_outreach_api(outreach_id: int):
    try:
        result = send_outreach_by_id(outreach_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "message": result}


@app.post("/campaigns/{campaign_id}/send")
async def send_campaign_api(campaign_id: int):
    try:
        result = send_campaign(campaign_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", **result}


@app.get("/intents")
async def list_intents_api(limit: int = 100):
    rows = list_intent_rows(limit=limit)
    return {"count": len(rows), "intents": rows}


@app.delete("/intents/{intent_id}")
async def delete_intent_api(intent_id: int):
    ok = remove_intent(intent_id)
    if not ok:
        raise HTTPException(status_code=404, detail="意图识别记录不存在")
    return {"status": "deleted", "intent_id": intent_id}


@app.delete("/intents")
async def delete_all_intents_api():
    count = remove_all_intents()
    return {"status": "ok", "deleted_count": count}


@app.get("/leads")
async def list_leads_api():
    rows = list_lead_rows()
    return {"count": len(rows), "leads": rows}


@app.delete("/leads/{lead_id}")
async def delete_lead_api(lead_id: int):
    ok = remove_lead(lead_id)
    if not ok:
        raise HTTPException(status_code=404, detail="工单不存在")
    return {"status": "deleted", "lead_id": lead_id}


@app.delete("/leads")
async def delete_all_leads_api():
    count = remove_all_leads()
    return {"status": "ok", "deleted_count": count}


@app.patch("/leads/{lead_id}/status")
async def patch_lead_status_api(lead_id: int, payload: dict):
    status = payload.get("status", "")
    updated = patch_ticket_status(lead_id, status)
    if not updated:
        raise HTTPException(status_code=400, detail="工单不存在或状态值无效（允许：new/in_progress/done）")
    return {"status": "ok", "ticket": updated}


@app.get("/leads/export")
async def export_leads_api():
    csv_text = export_leads_csv()
    headers = {"Content-Disposition": 'attachment; filename="collaboration_tickets.csv"'}
    return PlainTextResponse(csv_text, headers=headers, media_type="text/csv; charset=utf-8-sig")


@app.get("/kols")
async def list_kols():
    threads = list_all_threads()
    return {"count": len(threads), "kols": threads}


@app.get("/processed")
async def list_processed(limit: int = 100):
    records = list_processed_messages(limit=limit)
    return {"count": len(records), "records": records}


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


@app.delete("/all-data")
async def delete_all_api():
    result = delete_all_data()
    logger.warning(f"🗑️ 已清空全部业务数据: {result}")
    return {"status": "cleared", **result}


@app.get("/logs")
async def get_logs(tail: int = 80):
    logs = list(_log_buffer)[-tail:]
    return {"count": len(logs), "logs": logs}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    if not _dashboard_path.exists():
        raise HTTPException(status_code=500, detail="dashboard 文件不存在")
    return HTMLResponse(_dashboard_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
