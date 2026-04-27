"""
llm_service.py — 大语言模型调用层（客服场景）

核心能力：
  1. call_llm()                          通用 LLM HTTP 调用（兼容 OpenAI Chat Completions 格式）
  2. detect_customer_satisfaction_and_tone()  分析来信情绪与语气
  3. generate_calm_reply()               生成安抚/共情回复
  4. generate_normal_reply()             生成普通客服回复
  5. generate_escalation_summary()       生成内部升级通知摘要
  6. translate_to_chinese_for_support()  将用户原文译成中文（供内部升级邮件）
"""

import json
import logging
import requests
from typing import Any

from app.config import config

logger = logging.getLogger(__name__)


# ─── 底层 LLM 调用 ─────────────────────────────────────────────────────────────

def call_llm(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 1500,
) -> str:
    """
    调用大语言模型 API（标准 OpenAI Chat Completions 格式）。

    更换服务商只需修改 .env 中的 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL：
      - OpenAI GPT-4o:   LLM_BASE_URL=https://api.openai.com/v1
      - 通义千问 Plus:   LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
      - DeepSeek:        LLM_BASE_URL=https://api.deepseek.com/v1
      - 本地 Ollama:     LLM_BASE_URL=http://localhost:11434/v1
    """
    url = f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _clean_json_block(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[len("```json"):].strip()
    elif text.startswith("```"):
        text = text[len("```"):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


# ─── 情绪与语气检测 ────────────────────────────────────────────────────────────

def detect_customer_satisfaction_and_tone(
    contact: dict,
    latest_message: str,
    thread_history: list[dict],
) -> dict:
    """
    分析用户来信的情绪与语气。

    Returns:
        {
            "sentiment": "satisfied" | "neutral" | "dissatisfied",
            "tone": "cooperative" | "firm" | "hostile",
            "escalate_recommended": bool,
            "reason_short": str
        }
    """
    contact_name = contact.get("name") or contact.get("email") or "客户"
    history_lines = []
    for item in thread_history[-6:]:
        role = "客服" if item.get("is_mine") else contact_name
        history_lines.append(f"[{role}] {item.get('body', '')[:260].replace(chr(10), ' ')}")
    history_text = "\n".join(history_lines) or "（无历史记录）"

    system_prompt = """你是专业的客服情绪分析助手。

请根据用户来信内容，分析其情绪满意度与语气，并只返回 JSON：
{
  "sentiment": "satisfied|neutral|dissatisfied",
  "tone": "cooperative|firm|hostile",
  "escalate_recommended": true|false,
  "reason_short": "一句中文原因"
}

定义：
- sentiment:
  - satisfied: 表示满意、感谢、问题已解决
  - neutral: 普通咨询、无明显情绪
  - dissatisfied: 不满、抱怨、失望、愤怒
- tone:
  - cooperative: 友善、配合、语气平和
  - firm: 强调立场、语气坚决但不失礼
  - hostile: 威胁、骂人、使用极端词汇
- escalate_recommended: 当 dissatisfied 且 tone 为 firm/hostile，或涉及退款/法律/媒体时，建议升级

只输出 JSON，不要任何额外解释。"""

    user_prompt = f"""联系人：{contact_name}

近期对话历史：
{history_text}

最新来信：
{latest_message[:1500]}"""

    try:
        raw = call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        result = json.loads(_clean_json_block(raw))
        sentiment = result.get("sentiment", "neutral")
        if sentiment not in ("satisfied", "neutral", "dissatisfied"):
            sentiment = "neutral"
        tone = result.get("tone", "cooperative")
        if tone not in ("cooperative", "firm", "hostile"):
            tone = "cooperative"
        return {
            "sentiment": sentiment,
            "tone": tone,
            "escalate_recommended": bool(result.get("escalate_recommended", False)),
            "reason_short": result.get("reason_short", ""),
        }
    except Exception as exc:
        logger.warning(f"⚠️ 情绪检测失败，使用规则兜底: {exc}")

    # 规则兜底
    text = (latest_message or "").lower()
    hostile_keywords = ["律师", "起诉", "sue", "lawyer", "媒体", "media", "投诉", "complaint",
                        "骗子", "scam", "fraud", "garbage", "terrible", "horrible"]
    dissatisfied_keywords = ["不满意", "失望", "差劲", "退款", "refund", "broken", "damaged",
                             "not working", "disappointed", "angry", "upset", "问题", "故障"]

    if any(kw in text for kw in hostile_keywords):
        return {"sentiment": "dissatisfied", "tone": "hostile", "escalate_recommended": True, "reason_short": "命中强硬/威胁关键词"}
    if any(kw in text for kw in dissatisfied_keywords):
        return {"sentiment": "dissatisfied", "tone": "firm", "escalate_recommended": False, "reason_short": "命中不满关键词"}
    return {"sentiment": "neutral", "tone": "cooperative", "escalate_recommended": False, "reason_short": "规则兜底默认中性"}


# ─── 回复生成 ──────────────────────────────────────────────────────────────────

def generate_calm_reply(
    contact: dict,
    product: dict | None,
    sentiment: str,
    tone: str,
    latest_message: str,
    thread_history: list[dict],
    *,
    after_sales_notified: bool,
) -> str:
    """
    生成安抚/共情客服回复（dissatisfied / firm / hostile）。

    after_sales_notified：为 True 时**才可**在正文中写「已联系/同步售后」等表述（与系统已发出的内部通知一致）；
    为 False 时不得声称已向售后发单，但仍须安抚并索取订单与问题信息。
    """
    contact_name = contact.get("name") or "您"
    product_name = (product or {}).get("name") or "您购买的产品"
    history_lines = []
    for item in thread_history[-4:]:
        role = "客服" if item.get("is_mine") else contact_name
        history_lines.append(f"[{role}] {item.get('body', '')[:200].replace(chr(10), ' ')}")
    history_text = "\n".join(history_lines) or "（首次联系）"

    compensation_instruction = (
        "- 可以视情况提及具体补偿方案（如退款、换货），但需有实质内容，不可空洞承诺"
        if config.ALLOW_COMPENSATION_PROMISES
        else "- 不要承诺具体赔偿金额或无条件退款，说明会转交处理或跟进即可"
    )

    escalation_note = ""
    if tone == "hostile":
        escalation_note = "- 对方语气激烈，仍须保持谦抑、零对抗，用尊重化解情绪"

    if after_sales_notified:
        as_block = f"""
【事实约束】系统已向售后/产品负责人发出内部通知。你必须在正文中**明确表述**已同步售后团队、对方可在约 1–2 个工作日（business days）内获得跟进。不得暗示尚未联系。"""
    else:
        as_block = f"""
【事实约束】未向售后发出内部通知（例如系统无法投递）。正文中**禁止**写「已联系售后」「已转交售后」等已办妥表述。可写：我们已记录并将尽快由支持团队处理；并请客户补充信息以便处理。可写预计在合理时间内回复，勿编造已发单给售后。"""

    system_prompt = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员，采用「极度谦恭、感恩、主动担责」的邮件风格（真诚、把客户放在第一位；避免油腻）。

回复结构（顺序不可省略核心步骤）：
1. 先安抚与共情，真诚致歉（针对来信中具体不满点呼应）。
2. **务请客户补充以下信息**（用列表或分条，与客户语言一致）：
   - 订单编号 / Order or purchase reference number
   - 产品信息（如产品名称、SKU/型号、购买渠道、套装规格等）/ Product name, SKU, where purchased
   - 问题或异常的清晰描述 / A clear description of the issue
3. 说明在收到上述信息后，售后或专人能更精准地处理。
4. 语气：极度友好、谦卑、不推诿。

{as_block}
{compensation_instruction}
{escalation_note}

其它要求：
- 语言镜像：与客户来信主语言一致。
- 须针对来信内容具体回应，禁止空泛套话。
- 长度：英文约 150–280 词；中文约 260–520 字。
- 结尾署名：{config.BRAND_SIGNATURE}
- 只输出回复正文，不要主题行与标注。"""

    user_prompt = f"""联系人：{contact_name}
产品：{product_name}
情绪：{sentiment} | 语气：{tone}
after_sales_notified（系统标志）: {after_sales_notified}

近期对话：
{history_text}

客户最新来信：
{latest_message[:1200]}

请生成安抚与索取信息的回复。"""

    try:
        return call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=900,
        )
    except Exception as exc:
        logger.warning(f"⚠️ 安抚回复生成失败，使用模板兜底: {exc}")
        if after_sales_notified:
            return (
                f"Dear {contact_name},\n\n"
                f"We are truly sorry for the frustration you’ve had with {product_name}, and we appreciate you writing to us.\n\n"
                f"To help our after-sales team move quickly, could you please reply with:\n"
                f"(1) Your order or purchase reference number\n"
                f"(2) Product details (name/SKU, where you purchased)\n"
                f"(3) A clear description of the issue you’re facing\n\n"
                f"We have already notified our after-sales team. You can expect a follow-up within 1–2 business days.\n\n"
                f"{config.BRAND_SIGNATURE}"
            )
        return (
            f"Dear {contact_name},\n\n"
            f"We are truly sorry for the trouble with {product_name}, and we appreciate you reaching out.\n\n"
            f"To help us assist you, please reply with: (1) your order or purchase reference, (2) product name/SKU and purchase channel, "
            f"(3) a short description of the problem.\n\n"
            f"We have recorded your message and will have our support team follow up with you as soon as possible.\n\n"
            f"{config.BRAND_SIGNATURE}"
        )


def generate_normal_reply(
    contact: dict,
    product: dict | None,
    latest_message: str,
    thread_history: list[dict],
) -> str:
    """
    生成普通客服回复。用于 satisfied / neutral + cooperative 场景（非安抚主流程，不要求固定售后话术）。
    """
    contact_name = contact.get("name") or "您"
    product_name = (product or {}).get("name") or "您的订单"
    history_lines = []
    for item in thread_history[-4:]:
        role = "客服" if item.get("is_mine") else contact_name
        history_lines.append(f"[{role}] {item.get('body', '')[:200].replace(chr(10), ' ')}")
    history_text = "\n".join(history_lines) or "（首次联系）"

    system_prompt = f"""你是 {config.BRAND_NAME} 品牌的客服专员。采用谦恭、专业的邮件风格。

回复要求：
1. 语言镜像：与客户来信语言一致。
2. 必须针对来信：直接回应问题、感谢或需求，对应原文要点。
3. 语气：温暖、专业；不夸大、不承诺无法兑现的赔偿（除非有明确政策）。
4. 长度：英文约 80–180 词；中文约 150–320 字。
5. 结尾署名：{config.BRAND_SIGNATURE}
6. 只输出回复正文。"""

    user_prompt = f"""联系人：{contact_name}
产品：{product_name}

近期对话：
{history_text}

客户来信：
{latest_message[:1200]}

请生成客服回复。"""

    try:
        return call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            max_tokens=700,
        )
    except Exception as exc:
        logger.warning(f"⚠️ 普通客服回复生成失败，使用模板兜底: {exc}")
        return (
            f"Dear {contact_name},\n\n"
            f"Thank you for your message about {product_name}. We have received it and will get back to you as soon as we can.\n\n"
            f"{config.BRAND_SIGNATURE}"
        )


# ─── 用户原文 → 中文（内部升级邮件用）──────────────────────────────────────────

def translate_to_chinese_for_support(text: str, max_chars: int = 2000) -> str:
    """
    将用户来信任意语种译为简体中文，供中文客服阅读。

    若 API 失败则返回空字符串，由邮件模板提示人工阅读原文。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    raw = raw[:max_chars]
    system_prompt = (
        "你是专业译员，面向客服团队内部使用。\n"
        "请将用户来信完整译为**简体中文**。\n"
        "要求：忠实原意、保留语气与情绪（包括不满、俚语、粗口等，可用委婉中文表述但勿弱化严重性）。\n"
        "若原文已是通顺的简体中文，则保持或仅做极轻微标点后输出，不要删改信息。\n"
        "只输出译文正文，不要任何前言、后注或引号包裹。"
    )
    try:
        return call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": raw},
            ],
            temperature=0.2,
            max_tokens=min(2000, max(800, len(raw) * 2)),
        )
    except Exception as exc:
        logger.warning(f"⚠️ 用户原文译中文失败: {exc}")
        return ""


# ─── 内部升级摘要 ──────────────────────────────────────────────────────────────

def generate_escalation_summary(
    thread_id: str,
    contact: dict,
    product: dict | None,
    latest_message: str,
    sentiment: str,
    tone: str,
    reason: str,
) -> str:
    """
    生成发给产品负责人的内部升级通知摘要（≤5 行）。
    """
    contact_name = contact.get("name") or contact.get("email") or "未知联系人"
    contact_email = contact.get("email") or ""
    product_name = (product or {}).get("name") or "未绑定产品"

    system_prompt = """你是客服升级通知助手。请用中文生成一份简洁的内部升级摘要，供产品负责人快速了解情况。

格式（严格按此，共 5 行以内）：
联系人：[姓名] <[邮箱]>
产品：[产品名]
情绪/语气：[满意度] / [语气]
升级原因：[一句话原因]
原文摘要：[客户来信前100字]

只输出上述格式文本，不要其他内容。"""

    user_prompt = f"""线程ID：{thread_id}
联系人：{contact_name} <{contact_email}>
产品：{product_name}
情绪：{sentiment} | 语气：{tone}
升级触发原因：{reason}

客户来信：
{latest_message[:600]}"""

    try:
        return call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=300,
        )
    except Exception as exc:
        logger.warning(f"⚠️ 升级摘要生成失败，使用模板兜底: {exc}")
        excerpt = latest_message[:100].replace("\n", " ")
        return (
            f"联系人：{contact_name} <{contact_email}>\n"
            f"产品：{product_name}\n"
            f"情绪/语气：{sentiment} / {tone}\n"
            f"升级原因：{reason}\n"
            f"原文摘要：{excerpt}"
        )
