"""
llm_service.py — 大语言模型调用层（客服场景）

核心能力：
  1. call_llm()                          通用 LLM HTTP 调用（兼容 OpenAI Chat Completions 格式）
  2. detect_customer_satisfaction_and_tone()  分析来信情绪与语气
  3. generate_calm_reply()               生成安抚/共情回复
  4. generate_normal_reply()             生成普通客服回复（满意路径可极委婉邀评）
  5. generate_escalation_summary()       生成内部升级通知摘要
  6. translate_to_chinese_for_support()  将用户原文译成中文（供内部升级邮件）
"""

import json
import logging
import requests
from typing import Any

from app.config import config

logger = logging.getLogger(__name__)

# 对外客户邮件：全文英文 + 降低营销模板感，减轻 Gmail 等对「混语种 / 群发促销体」的误判。
CUSTOMER_REPLY_ENGLISH_ANTISPAM = """
- **对外用语（硬性）：** 给客户看的正文必须**全文英文**，不得出现中文、日文等非英文字符；禁止中英混排。混语种易被归类为异常邮件。
- **防垃圾邮件 / 避免营销模板感：** 写成真人同事发出的**简短事务邮件**，而非促销群发或话术填空模板。
  - 避免空洞套话堆砌（如过量使用 “deeply apologize for any inconvenience”“your satisfaction is our priority”、无实质的多段致歉）。
  - **不要用**促销邮件常见的刺眼编号清单体「(1)(2)(3)」或长项目符号块；索要订单号 / 产品 / 问题时用**两三句自然英文段落**，必要时仅用极简短行（如一行一个问题）。
  - 产品称呼：沿用客户来信里的叫法或简短中性说法即可；勿全文照搬冗长电商 SEO 标题（除非核对 SKU 必需）。
  - 少用全大写、少用多重感叹号；语气专业、直接、便于扫读。
"""


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
    product_resolved: bool = False,
    intense_appeasement: bool = False,
    calm_mode: str = "default",
) -> str:
    """
    生成安抚/共情客服回复（dissatisfied / firm / hostile）。

    after_sales_notified：为 True 时**才可**在正文中写「已联系/同步售后」等表述（与系统已发出的内部通知一致）；
    为 False 时不得声称已向售后发单，但仍须安抚并**优先**索取订单号（购买凭证）。
    product_resolved：在 **calm_mode=default** 下沿用原意（少问/必索 等，见下）；首通或收尾模式有单独约束。
    intense_appeasement：为 True 时表示语气 hostile / 情绪激烈，道歉与共情**优先**、篇幅可更足；为 False 时走「正常」不满反馈：专业、克制、以清晰索要必索项为主。

    calm_mode:
      - **initial_three**：本线程**第一封**我方正式回复，礼貌索要 **(1)订单编号/凭证 (2)产品名称 (3)问题描述**；已提供的项只确认、勿重复要。
      - **close_ack**：客户在我方去信**之后**、本信已能识别**单号+品名/产品指称**时收尾，**不要**再追问题描述，致谢+歉+依事实约束转售后/跟进。
      - **default**：多轮中其它情况，沿用与 product_resolved 相关的一套「少问」策略（见下 info_step）。
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

    if after_sales_notified:
        as_block = f"""
【事实约束】系统已向售后/产品负责人发出内部通知。你必须在正文中**明确表述**已同步售后团队、对方可在约 1–2 个工作日（business days）内获得跟进。不得暗示尚未联系。"""
    else:
        as_block = f"""
【事实约束】当前信息未齐（缺订单或产品），系统**暂未**向售后发送工单。
正文中**禁止**写「已联系售后」「已转交售后」等已办妥表述。
你必须**明确告诉客户**：我们已为您建立专属服务档案，为了让售后工程师能最快为您解决问题，请您回复提供下述信息，收到后我们将立刻安排专人接手处理。"""

    # ─── 子模式：多轮补「单号+品名」后的收尾，不再追问题描述 ───
    if calm_mode == "close_ack":
        if intense_appeasement:
            escalation_note = (
                "- 首段须**真诚致谢**并**继续充分致歉**（可呼应其此前不满），但**禁止**以清单/追问形式要对方**再写问题经过**；对过往/首封中已提诉求只承接、转交表述须符合【事实约束】。"
            )
        else:
            escalation_note = (
                "- 语气为**简明的收尾与致谢**：不冗长、不堆套话，**不**要「请再具体描述问题」等追问句。"
            )
        if intense_appeasement:
            style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。对方此前表达过不满，**本信已补充订单/单号与品名/产品信息**；你的任务是**致谢、致歉、说明跟进**，**不要**再追问**问题描述/故障细节**（首封或线程中已写清的不满请承接即可）。

回复结构（顺序不可省略）：
1. 充分感谢对方补充资料；对不便与经历表示真诚歉意与重视。
2. 承接对方诉求类型（如退款/质量），**不**要对方重述长文问题。
3. 按【事实约束】写清是否已交售后/预计跟进，不得与系统状态矛盾；**不**在正文列点索要**更多问题说明**。

{as_block}
{compensation_instruction}
{escalation_note}

4. 语气：谦抑、可信赖。篇幅英文约 130–260 词为宜。"""
        else:
            style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。对方**本信已提供单号+品名/产品**；**请勿再追问问题描述**。

回复结构（顺序不可省略）：
1. 感谢其订单/单号与品名/产品信息，简短致歉、承接其诉求方向。
2. 按【事实约束】写跟进与售后/记录说明；**禁止**以提问形式要「请再具体描述/补充故障经过」等。

{as_block}
{compensation_instruction}
{escalation_note}

3. 语气：专业、温暖、不施压。篇幅英文约 110–220 词。"""

        other_common = f"""
其它要求：
{CUSTOMER_REPLY_ENGLISH_ANTISPAM}
- **本模式禁止**向客户**追加**索要**问题经过/问题描述/故障说明**；若其早期来信中已有不满，只表示感谢已记录并会一并转达。
- 须针对其最新来信**具体**致谢，禁止空泛套话。
- 长度：以简洁为主；激烈场景可略长但仍**不**追问题材料。
- 结尾署名：{config.BRAND_SIGNATURE}
- 只输出回复正文，不要主题行与标注。"""
        system_prompt = f"""{style_block}
{other_common}"""

        user_prompt = f"""联系人：{contact_name}
产品（系统侧识别）: {product_name}
情绪：{sentiment} | 语气：{tone}
intense_appeasement: {intense_appeasement}
after_sales_notified: {after_sales_notified}
calm_mode: close_ack（**收尾、勿追问题**）

近期对话：
{history_text}

客户最新来信：
{latest_message[:1200]}

请生成**收尾**安抚回复：致谢单号+品名、道歉、按【事实约束】转售后/跟进，**不要**再索要问题描述。"""

        try:
            return call_llm(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=800,
            )
        except Exception as exc:
            logger.warning(f"⚠️ 安抚回复生成失败，使用模板兜底: {exc}")
            if intense_appeasement:
                open_apology = (
                    f"We’re truly sorry for the frustration this has caused. Thank you for sending your order and product details—we’ve noted everything carefully."
                )
            else:
                open_apology = (
                    f"Hello {contact_name}, thank you for your order and product details, and we’re sorry for the inconvenience."
                )
            if after_sales_notified:
                return (
                    f"Dear {contact_name},\n\n"
                    f"{open_apology}\n\n"
                    f"We’ve logged your information and notified after-sales. A teammate should reach out within about 1–2 business days. We won’t ask you to repeat your issue description again.\n\n"
                    f"{config.BRAND_SIGNATURE}"
                )
            return (
                f"Dear {contact_name},\n\n"
                f"{open_apology}\n\n"
                f"We’ve recorded your order and product details and our support team will follow up shortly. We won’t ask you to repeat the problem description here; reply anytime on this thread if something new comes up.\n\n"
                f"{config.BRAND_SIGNATURE}"
            )

    # ─── 子模式：本线程首封我方正式回复，索要 (订单+品名+问题描述) ───
    if calm_mode == "initial_three":
        if product_resolved and product:
            info_step = f"""2. 本线程**首次**由我方**正式**回复。可关联产品「{product_name}」。
   用**简短英文段落**说明尚需哪些材料（**来信/历史中已写清的项，只感谢承接，勿重问**）：订单编号或购买凭证；产品名称/规格（可请对方确认是否与「{product_name}」一致）；问题描述（若对方已写清则承接即可，勿要求其「再描述一遍」）。勿用 (1)(2)(3) 营销清单体。"""
        else:
            info_step = """2. 本线程**首次**由我方**正式**回复。用**简短英文段落**说明尚需（**已提供项只确认、勿重问**）：订单编号或凭证；产品名称（型号/渠道等便于定位）；问题与诉求（若对方已写清则承接，勿要求其重复叙述）。勿用 (1)(2)(3) 营销清单体。"""

        if intense_appeasement:
            escalation_note = (
                "- 对方情绪可能激烈：正文**前半**须**充分、真诚**致歉与共情，承认对方感受；**然后**再过渡到上述三项，语气尊重、不辩解。"
            )
        else:
            escalation_note = (
                "- 共情后用**英文自然段落**说明尚需的三类信息（订单、产品指称、问题），勿用促销邮件式编号清单；让对方清楚为何需要这些信息即可。"
            )

    # ─── 默认多轮：沿用「少问」与 product_resolved 分支 ───
    else:
        if intense_appeasement:
            escalation_note = (
                "- 对方情绪激烈：正文**前半**须充分、真诚致歉与共情，承认对方感受、零辩解、零对抗；"
                "仍须保持专业与尊重边界；后再自然过渡到下方必索项（订单号/凭证等）。"
            )
        else:
            escalation_note = (
                "- 对方属一般不满/反馈：语气温和专业，**不要**逐句「卑躬屈膝」或过度排比致歉；"
                "共情 1–2 句即可，接着清晰列出**必索**订单/凭证，避免冗长"
            )
        if product_resolved and product:
            info_step = f"""2. 我们已根据来信与记录关联到产品「{product_name}」，**请勿再向客户索要完整产品名/SKU/购买渠道**（除非对方描述与该产品明显不符，可委婉请其确认）。
   **必索项仅一项**：订单编号或购买凭证；若来信/历史中已提供，仅致谢确认，**绝不再要**。
   **问题或异常为可选**：对方已说清时致谢承接即可，**禁止**再列点追问「请进一步描述」；仅当对方几乎未提及时，可**一句**邀请补充，勿反复。
3. 可简要说明有订单后售后能更快处理（勿暗示「必须同时」提供问题长文）。"""
        else:
            info_step = """2. **请客户补充信息**（用英文自然段落或极简短句；已提供的项只确认、勿重复索要）：必索订单编号；产品信息在其未说明时再问（名称/SKU/渠道等）；问题细节若已描述则勿再追问，几乎未提及时可一句带过。
3. 说明有订单后便于售后处理，勿给「三项都必须填完」的压迫感；勿用促销邮件式项目符号大块列举。"""

    if calm_mode in ("default", "initial_three"):
        if intense_appeasement:
            style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。当前为**高冲突、情绪激烈**场景：采用**极度谦恭、优先道歉、先抚情绪再办正事**的邮件风格（真诚、不推诿、避免油腻与敷衍）。

回复结构（顺序不可省略核心步骤）：
1. 开头用充足篇幅**道歉与共情**（具体呼应对方不满点），再过渡到索取/沟通的信息（见下第 2 点）。
{info_step}
4. 语气：极度友好、愿意承担、把对方放在第一位。

{as_block}
{compensation_instruction}
{escalation_note}"""
        else:
            style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。当前为**一般不满/正常反馈**场景：专业、温暖、有担责，但**不**用夸张「跪舔」体；以清晰、可执行为主。

回复结构（顺序不可省略核心步骤）：
1. 简短、真诚的共情与致歉（1–2 段内），**尽快**进入下述沟通要点，列表清晰、易回复（若上段已呼应不满，可略短）。
{info_step}
4. 语气：友好、专业、不卑不亢、不堆砌重复致歉。

{as_block}
{compensation_instruction}
{escalation_note}"""

        extra_antiduplicate = (
            "- **防重复**：来信或近期历史中已有订单号 → 不再次索要，仅感谢确认；已描述问题/不满 → 不再次「请具体说明/进一步描述」。"
            if calm_mode == "default"
            else (
                "- **防重复**（首通三项）：(1)–(3) 任一项在来信或**近期历史**中**已给齐/已写清** → 只**致谢确认**，**禁止**再逐条要同一内容；可说明「已收悉将一并转交」。"
            )
        )
        len_hint = (
            "激烈场景英文约 180–320 词；一般反馈英文约 120–250 词。"
            if calm_mode == "default"
            else "首次正式回复可略长以便交代所需信息；激烈场景英文约 180–340 词，一般约 140–280 词。"
        )

        system_prompt = f"""{style_block}

其它要求：
{CUSTOMER_REPLY_ENGLISH_ANTISPAM}
- 须针对来信内容具体回应，禁止空泛套话。
{extra_antiduplicate}
- 长度：{len_hint}
- 结尾署名：{config.BRAND_SIGNATURE}
- 只输出回复正文，不要主题行与标注。"""

        user_extra = (
            "请按【首通三项】与防重复要求生成，缺哪类再索哪类；**勿**在对方已给齐时继续要。"
            if calm_mode == "initial_three"
            else "已有所述信息勿重复问；**仅缺订单号**时可明确索取订单号。"
        )
        user_prompt = f"""联系人：{contact_name}
产品：{product_name}
情绪：{sentiment} | 语气：{tone}
intense_appeasement: {intense_appeasement}
after_sales_notified: {after_sales_notified}
calm_mode: {calm_mode}

近期对话：
{history_text}

客户最新来信：
{latest_message[:1200]}

{user_extra}"""

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
            if calm_mode == "initial_three" and after_sales_notified:
                return (
                    f"Dear {contact_name},\n\n"
                    f"We’re sorry for the trouble. To move quickly, please reply with your order or purchase proof, the product name/model, and a brief description of what went wrong "
                    f"(skip anything you already explained above).\n\n"
                    f"We’ve already looped in after-sales; someone should contact you within about 1–2 business days.\n\n"
                    f"{config.BRAND_SIGNATURE}"
                )
            if calm_mode == "initial_three" and not after_sales_notified:
                return (
                    f"Dear {contact_name},\n\n"
                    f"We’re sorry this happened. We’ve opened a ticket for you—please reply with your order or purchase proof, the product name/model, and a short summary of the issue "
                    f"(confirm only if you already covered it in your last message).\n\n"
                    f"Once we have that, we’ll assign someone to take it forward.\n\n"
                    f"{config.BRAND_SIGNATURE}"
                )
            if product_resolved and product:
                ask_block_notified = (
                    "To help our after-sales team, please reply with your order or purchase reference number "
                    "(this is the one detail we need to locate your case). If anything about the issue still needs clarification, "
                    "you’re welcome to add a line—otherwise we won’t ask you to repeat what you’ve already explained.\n\n"
                )
                ask_block_plain = (
                    "To help us assist you, please send your order or purchase reference number. "
                    "If you’ve already described the problem above, there’s no need to repeat it.\n\n"
                )
            else:
                ask_block_notified = (
                    "To help our after-sales team, please send your order or purchase reference number (required). "
                    "If you can also share the product name/SKU and where you purchased, that helps; "
                    "a fuller issue description is helpful only if it isn’t already in your message—we won’t ask you to explain twice.\n\n"
                )
                ask_block_plain = (
                    "Please send your order or purchase reference (required). "
                    "Product name/channel helps if not yet clear. "
                    "You don’t need to re-describe the problem if you’ve already covered it in this thread.\n\n"
                )
            if intense_appeasement:
                open_apology = (
                    f"We are deeply sorry for the upsetting experience you’ve had, and for any distress this has caused. "
                    f"That is not the standard we want for {product_name}, and we take your feedback with full seriousness.\n\n"
                )
            else:
                open_apology = (
                    f"We are sorry for the trouble with {product_name}, and thank you for letting us know.\n\n"
                )
            if after_sales_notified:
                return (
                    f"Dear {contact_name},\n\n"
                    f"{open_apology}"
                    f"{ask_block_notified}"
                    f"We have already notified our after-sales team. You can expect a follow-up within 1–2 business days.\n\n"
                    f"{config.BRAND_SIGNATURE}"
                )
            return (
                f"Dear {contact_name},\n\n"
                f"{open_apology}"
                f"{ask_block_plain}"
                f"We have opened a dedicated service ticket for you. Once we receive these details, we will immediately assign a specialist to handle your case.\n\n"
                f"{config.BRAND_SIGNATURE}"
            )

    return ""


def generate_normal_reply(
    contact: dict,
    product: dict | None,
    latest_message: str,
    thread_history: list[dict],
    sentiment: str = "neutral",
) -> str:
    """
    生成普通客服回复。用于 satisfied / neutral + cooperative 场景（非安抚主流程，不要求固定售后话术）。
    sentiment 为 satisfied 时，在结尾极委婉加入「若愿意分享体验或评价，或能帮助其他用户参考」类表述，须简短、零施压、非硬性要求。
    """
    contact_name = contact.get("name") or "您"
    product_name = (product or {}).get("name") or "您的订单"
    history_lines = []
    for item in thread_history[-4:]:
        role = "客服" if item.get("is_mine") else contact_name
        history_lines.append(f"[{role}] {item.get('body', '')[:200].replace(chr(10), ' ')}")
    history_text = "\n".join(history_lines) or "（首次联系）"

    satisfied_extra = ""
    if sentiment == "satisfied":
        satisfied_extra = """
7. 【满意/致谢类来信专属】正文**最后一段之前**用**一两句（英文）**极委婉、可选的收尾（例如：if you ever leave a brief honest note where you purchased, it can help others decide—entirely optional, no pressure）。禁止命令式、禁止索要好评截图、禁止占过长篇幅。"""

    system_prompt = f"""你是 {config.BRAND_NAME} 品牌的客服专员。采用谦恭、专业的邮件风格。

回复要求：
1. {CUSTOMER_REPLY_ENGLISH_ANTISPAM}
2. 必须针对来信：感谢用户的反馈或直接回应需求。但对于具体的产品操作、功能设置、技术指导等“How-to”问题，**绝对不要自行编造或猜测操作步骤**。应当礼貌地告知客户：已将该咨询转交至技术/产品团队，他们会尽快提供准确的操作指引。
3. 语气：温暖、专业；不夸大、不承诺无法兑现的赔偿（除非有明确政策）。
4. 长度：英文约 80–180 词。
5. 结尾署名：{config.BRAND_SIGNATURE}
6. 只输出回复正文。
{satisfied_extra}"""

    user_prompt = f"""联系人：{contact_name}
产品：{product_name}
情绪标签（供策略参考）：{sentiment}

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

    user_prompt = f"""会话 ID：{thread_id}
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
