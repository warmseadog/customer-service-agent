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
  - **篇幅：** 能说清则**尽量短**；避免多段重复致歉或同义铺开；多数场景以约 **90–200 英文词**为佳（激烈安抚可至约 220 词上限，仍忌冗长）。
"""

# 退款/赔偿：自动化对外回复不得代为终审承诺；统一移交售后并多角度安抚。
CUSTOMER_REPLY_REFUND_COMPENSATION_BOUNDARY = """
- **退款与赔偿（硬性）：** **绝对不得**代表公司**答应、同意、批准或否决**客户的退款、赔偿、补偿金额、到账日期或具体方案；禁止 “We approve your refund”“We agree to compensate …”“You will receive $…” 等易被理解为终审或财务承诺的表述。
- **移交售后：** 写明由 **after-sales（售后）团队**受理、审核与跟进个案即可；自动化回复**不作**赔偿/退款层面的终审承诺。
- **禁止渠道免责式否定（硬性，英文正文）：** **不要**写 “I/we cannot authorize or process refunds through this email/channel”“this channel is not able to finalize refunds” 等突出**本邮箱/本自动化通道无权**、易被读成推诿的句子。用 **after-sales will review and contact you** 等**正面表述**即可；**不要**用「无法在此处理退款」来做补充免责声明。
- **肯定与安抚（忌冗长）：** 须认可对方合理关切与感受，可从**两三个角度**简练表达理解与重视；**共情与致歉控制在简明篇幅内**，勿堆叠长段同义套话。
- **问题/性能等细节：** 对方**已写清**则只**承接致谢**，**禁止** *If you could…*、*We would appreciate if you could…*、*should you wish to…* 等**带「如果」的请求或二次催促**。尚缺核对项时仅用**一句**平实陈述尚可补充哪些信息便于跟进；**不给也不催**，未补不施压。
- **与【事实约束】一致：** 若系统尚未向售后发单，不得谎称已移交；可说明诉求已记录、收齐信息后将转交售后处理。
"""

# 「empathy_pure」对外：不写重复售后时间表/工单套话（内部仍可已发工单）
CUSTOMER_REPLY_EMPATHY_PURE_ONLY = """
- **财务与控制（硬性）：** **不得**代表公司同意、批准或否决退款/赔偿金额、到账日期；不得写易被理解为终审的句子（如 “We approve your full refund”“You will receive $…”）。
- **禁止空话套话：** **不得**复述或拼凑「passed to after-sales」「specialists are reviewing」「1–2 business days」「ticket / case escalation」等在**长篇往来中可能已经反复出现过的流程说明**。本封要**读起来像在对人说话**，不是再贴一遍状态通知。
- **回应重点：** **优先紧扣对方最新一封信的字面**：例如难过、是否在跟真人对话、被骂后的感受等；用简短、体面、温暖的英文。**避免**机械的 “I am a real human / I am not a bot” 口号式第一句——可读起来自然像一个名字签在邮件底下的同事。**篇幅**英文约 **70–130 词**。
"""


def _product_label(product: dict | None, *, default: str = "") -> str:
    """产品展示名：若有 brand 则「品牌 · 品名」，否则用品名。"""
    if not product:
        return default
    name = (product.get("name") or "").strip()
    brand = (product.get("brand") or "").strip()
    if brand and name:
        return f"{brand} · {name}"
    return name or default


# ─── 底层 LLM 调用 ─────────────────────────────────────────────────────────────

def call_llm(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 1500,
) -> str:
    """
    调用大语言模型 API（标准 OpenAI Chat Completions 格式）。

    更换服务商只需修改 .env 中的 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL：
      - OpenRouter:      LLM_BASE_URL=https://openrouter.ai/api/v1
                         LLM_MODEL=google/gemini-3.1-pro-preview（或其它 OpenRouter slug）
      - OpenAI GPT-4o:   LLM_BASE_URL=https://api.openai.com/v1
      - 通义千问 Plus:   LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
      - DeepSeek:        LLM_BASE_URL=https://api.deepseek.com/v1
      - 本地 Ollama:     LLM_BASE_URL=http://localhost:11434/v1

    使用 OpenRouter 时可在 .env 中设置 LLM_HTTP_REFERER、LLM_APP_TITLE（对应 HTTP-Referer /
    X-OpenRouter-Title，可选自愿头）。
    """
    api_key = (config.LLM_API_KEY or "").strip()
    if not api_key:
        raise ValueError(
            "LLM_API_KEY 为空：请在项目根目录 .env 中设置 LLM_API_KEY=sk-or-v1-…，保存后重启进程。"
            "（仅改编辑器未保存到磁盘时，运行中的 python 读不到密钥，OpenRouter 会报 401 Missing Authentication。）"
        )
    url = f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    ref = (config.LLM_HTTP_REFERER or "").strip()
    if ref:
        headers["HTTP-Referer"] = ref
    title = (config.LLM_APP_TITLE or "").strip()
    if title:
        headers["X-OpenRouter-Title"] = title
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
- escalate_recommended: 当 dissatisfied 且 tone 为 firm/hostile，或涉及**明确要求退款/赔偿/补偿金额**、法律/媒体/投诉升级时，建议升级（便于人工售后介入）

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
    compensation_claim_keywords = ["赔偿", "补偿", "索赔", "赔付", "退款", "退钱", "refund",
                                   "compensation", "reimburse", "compensate", "damages", "全额退款"]

    if any(kw in text for kw in hostile_keywords):
        return {"sentiment": "dissatisfied", "tone": "hostile", "escalate_recommended": True, "reason_short": "命中强硬/威胁关键词"}
    if any(kw in text for kw in compensation_claim_keywords):
        return {"sentiment": "dissatisfied", "tone": "firm", "escalate_recommended": True, "reason_short": "涉及退款/赔偿/索赔诉求，建议升级"}
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
    **注意：** `calm_mode=empathy_pure` 时在正文中**不按**上述规则强制复述售后时间线；仍以事实与提示词控制为准。
    product_resolved：在 **calm_mode=default** 下沿用原意（少问/必索 等，见下）；首通或收尾模式有单独约束。
    intense_appeasement：为 True 时表示语气 hostile / 情绪激烈，道歉与共情**优先**、篇幅可更足；为 False 时走「正常」不满反馈：专业、克制、以清晰索要必索项为主。

    calm_mode:
      - **initial_three**：本线程**第一封**我方正式回复，礼貌索要 **(1)订单编号/凭证 (2)产品名称 (3)问题描述**；已提供的项只确认、勿重复要。
      - **empathy_pure**：本条之前我方回信已 ≥ CALM_EMPATHY_ONLY_MIN_PRIOR_OUTBOUND：对外**不重复**售后时间线话术，短篇共情、回应字面。**不套用**常规的 `after_sales_notified` 强制售后段落。
      - **close_ack**：客户在我方去信**之后**、本信已能识别**单号+品名/产品指称**时收尾，**不要**再追问题描述，致谢+歉+依事实约束转售后/跟进。
      - **soothe_focus**：客户来信已**至少第三封**（同线程）；**禁止**反复索要订单号/凭证，以复述肯定 + **多角度安抚**为主，按事实约束移交售后。
      - **default**：多轮中其它情况，沿用与 product_resolved 相关的一套「少问」策略（见下 info_step）。
    """
    contact_name = contact.get("name") or "您"
    product_name = _product_label(product, default="您购买的产品")
    history_lines = []
    for item in thread_history[-4:]:
        role = "客服" if item.get("is_mine") else contact_name
        history_lines.append(f"[{role}] {item.get('body', '')[:200].replace(chr(10), ' ')}")
    history_text = "\n".join(history_lines) or "（首次联系）"

    _comp_policy_extra = (
        "\n- **政策口径补充（ALLOW_COMPENSATION_PROMISES 开启时）：** 仍不得承诺具体金额或到账时间；至多概括「售后将依据公司政策评估可选方案」，最终决定须由售后沟通。"
        if config.ALLOW_COMPENSATION_PROMISES
        else ""
    )
    compensation_instruction = (
        CUSTOMER_REPLY_REFUND_COMPENSATION_BOUNDARY.strip() + _comp_policy_extra
    )

    if after_sales_notified:
        as_block = f"""
【事实约束】系统已向售后/产品负责人发出内部通知。你必须在正文中**明确表述**已同步售后团队、对方可在约 1–2 个工作日（business days）内获得跟进。不得暗示尚未联系。"""
    else:
        as_block = f"""
【事实约束】当前信息未齐（缺订单或产品），系统**暂未**向售后发送工单。
正文中**禁止**写「已联系售后」「已转交售后」等已办妥表述。
你必须**明确告诉客户**：我们已为您建立专属服务档案，为了让售后工程师能最快为您解决问题，请您回复提供下述信息，收到后我们将立刻安排专人接手处理。"""

    # 「多轮后纯共情」：不走 as_block，不复述售后时间与工单套话（内部逻辑仍可与 after_sales_notified 并行）
    if calm_mode == "empathy_pure":
        empathy_extra = (
            "\n- **ALLOW_COMPENSATION_PROMISES：** 同样不得承诺具体金额或到账时间。"
            if config.ALLOW_COMPENSATION_PROMISES
            else ""
        )
        style_block_ep = f"""你是 {config.BRAND_NAME} 的高级客服专员。与对方已来往多封信；对方可能仍处于强烈情绪中。

{CUSTOMER_REPLY_ENGLISH_ANTISPAM}
{CUSTOMER_REPLY_EMPATHY_PURE_ONLY.strip()}
{empathy_extra}
- 署名：正文末行 {config.BRAND_SIGNATURE}

只输出回复正文英文，不要标题行。"""
        user_prompt_ep = f"""联系人：{contact_name}
产品（上下文）: {product_name}
情绪：{sentiment} | 语气：{tone}

近期对话节选：
{history_text}

对方最新来信（请逐句重视）：
{latest_message[:1400]}
"""
        try:
            return call_llm(
                [
                    {"role": "system", "content": style_block_ep},
                    {"role": "user", "content": user_prompt_ep},
                ],
                temperature=0.45,
                max_tokens=480,
            )
        except Exception as exc:
            logger.warning(f"⚠️ empathy_pure 安抚回复生成失败，使用模板兜底: {exc}")
            return (
                f"Dear {contact_name},\n\n"
                f"I'm really sorry you're still hurting over this—it makes sense you'd feel shaken when things haven't felt settled yet.\n\n"
                f"What you're expressing matters, especially when you spell out how low this has felt. Someone is reading you carefully—not recycling the same checklist at you—and we’re not asking you to perform calmness for us.\n\n"
                f"Thank you for trusting us enough to stay in touch.\n\n"
                f"{config.BRAND_SIGNATURE}"
            )

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
1. 充分感谢对方补充资料；对不便与经历表示真诚歉意与重视；**多角度肯定**对方表达的关切。
2. 若涉及退款/赔偿：**肯定**其诉求会得到认真对待；**绝对不得**代为答应或否决金额/方案；写明由 **after-sales** 审核与跟进。**不**要对方重述长文。**禁止**强调本邮箱/自动化通道无权处理退款。
3. 按【事实约束】写清是否已交售后/预计跟进，不得与系统状态矛盾；**不**列点索要**更多问题说明**。

{as_block}
{compensation_instruction}
{escalation_note}

4. 语气：谦抑、可信赖。**英文篇幅约 90–170 词**。"""
        else:
            style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。对方**本信已提供单号+品名/产品**；**请勿再追问问题描述**。

回复结构（顺序不可省略）：
1. 感谢其订单/单号与品名/产品信息；**肯定**其反馈；**简短**致歉。若有退款/赔偿诉求：**不得**代为答应，写明 **after-sales** 将审核。**禁止**「本渠道无法处理退款」类表述。
2. 按【事实约束】写跟进；**禁止**以提问形式要「请再具体描述」等。

{as_block}
{compensation_instruction}
{escalation_note}

3. 语气：专业、温暖。**英文篇幅约 80–140 词**。"""

        other_common = f"""
其它要求：
{CUSTOMER_REPLY_ENGLISH_ANTISPAM}
- **本模式禁止**向客户**追加**索要**问题经过/问题描述/故障说明**；若其早期来信中已有不满，只表示感谢已记录并会一并转达。
- 须针对其最新来信**具体**致谢，禁止空泛套话。
- 长度：以**简洁**为主；激烈场景也不得堆套话。
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

请生成**收尾**安抚回复：**简练**致谢单号+品名、肯定与致歉、按【事实约束】移交售后；退款相关**不写**渠道无权处理；**不要**索要问题描述；**禁用** *If you could…* 句式。"""

        try:
            return call_llm(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
                max_tokens=700,
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
            info_step = f"""2. 本线程**首次**由我方**正式**回复。用**一两段简短英文**说明尚利于核对的内容（**来信/历史中已写清的项只感谢承接，勿重问**）：订单编号或购买凭证；产品与购买渠道指称（可轻点是否与「{product_name}」一致）；若对方**尚未**提过具体现象，可**一句**中性说明尚有哪些信息便于售后评估——**禁止** *If you could…* 式条件句，**禁止**催问「请再描述性能问题」。若来信索要退款/赔偿：**不得**代为答应，写明 **after-sales** 收齐材料后审核。勿用 (1)(2)(3) 营销清单体。"""
        else:
            info_step = """2. 本线程**首次**由我方**正式**回复。用**一两段简短英文**说明尚利于核对的信息（**已提供项只确认、勿重问**）：订单或凭证、产品/渠道指称、与对方已述内容的关系；**禁止**用 *If you could…*/*We would appreciate if…* 索要问题细节，**禁止**催补对方已写过的部分。若来信索要退款/赔偿：**不得**代为答应，写明 **after-sales** 审核。勿用 (1)(2)(3) 营销清单体。"""

        if intense_appeasement:
            escalation_note = (
                "- 对方情绪可能激烈：正文**前半**须真诚致歉与共情，**控制总长度**；然后过渡到上述信息，**禁止** *If you could…* 式追问。"
            )
        else:
            escalation_note = (
                "- 共情后用**英文自然段落**说明尚需的三类信息（订单、产品指称、问题），勿用促销邮件式编号清单；让对方清楚为何需要这些信息即可。"
            )

    elif calm_mode == "soothe_focus":
        if intense_appeasement:
            escalation_note = (
                "- 对方已多轮来信且情绪激烈：以**倾听与安抚**为主；**禁止**反复索要订单号；逐点肯定后移交 **after-sales**；**忌**长文；**禁止**渠道无权处理退款的表述。"
            )
        else:
            escalation_note = (
                "- 对方已至少第三轮沟通：**禁止**像前几轮那样反复聚焦索要订单信息；若来信或历史中曾出现过可核对线索，只致谢承接。"
                "正文以**复述对方关切 + 多角度安抚**为主，再说明售后跟进；语气稳重、不施压。"
            )
        info_step = """2. **本线程客户来信已达至少第三轮**：**禁止**反复索要订单编号、购买凭证或催促补材料（除非整段对话几乎无任何可跟进线索，且仅用**一句**中立说明为何需要单号——不得质问）。
   **优先**：呼应其**最新来信**；简练安抚；**禁止** *If you could…* 式补充要求。
3. 按【事实约束】说明已记录或移交 **after-sales**；赔偿/退款边界见下方条款。**总篇幅偏短优于偏长**。"""

    # ─── 默认多轮：沿用「少问」与 product_resolved 分支 ───
    else:
        if intense_appeasement:
            escalation_note = (
                "- 对方情绪激烈：**简洁**致歉与共情后开始必索项；**禁止**「本渠道无权处理退款」类表述；语气尊重、不辩解。"
            )
        else:
            escalation_note = (
                "- 对方属一般不满/反馈：语气温和专业；共情**一两句**即可，**忌**长段排比致歉；清晰说明尚须核对的信息（**禁止** *If you could…* 式追问）。"
            )
        if product_resolved and product:
            info_step = f"""2. 我们已根据来信与记录关联到产品「{product_name}」，**请勿再向客户索要完整产品名/SKU/购买渠道**（除非对方描述与该产品明显不符，可委婉请其确认）。
   **必索项仅一项**：订单编号或购买凭证；若来信/历史中已提供，仅致谢确认，**绝不再要**。
   **现象/问题细节**：对方已说清时**一句承接**即可，**禁止** *If you could…* 或再次催描述；几乎未提及时可**一句**平实说明尚可补充哪些观察，**不**施压。
3. 可一句说明有订单后售后能更快处理（勿暗示「必须同时」提交长文说明）。"""
        else:
            info_step = """2. **请客户补充信息**（英文自然短段；已提供的项只确认、勿重复索要）：必索订单编号；产品与渠道在其未说明时再**一句**带过；问题/现象已描述则**不接**「如果愿意请再…」式补充，几乎未提及时**一句**中性说明即可。
3. 有订单便于售后处理即可，勿给「三项都必须填完」的压迫感。"""

    if calm_mode in ("default", "initial_three", "soothe_focus"):
        if calm_mode == "soothe_focus":
            if intense_appeasement:
                style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。当前为客户在本线程已**至少第三轮来信**：不要再像前几轮那样反复索要订单信息；以**复述承接 + 简练安抚**为主。

回复结构（顺序不可省略核心步骤）：
1. **简练**致歉、肯定对方关切（**一到两段**，忌冗长）；**严禁**质问式索要订单号。**禁止**「本邮箱无法处理退款」类表述及 *If you could…* 式补充要求；涉及退款/赔偿时**不得**代为答应，须移交 **after-sales**。
{info_step}
4. 语气：真诚、让对方感到被认真倾听。

{as_block}
{compensation_instruction}
{escalation_note}"""
            else:
                style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。当前为客户在本线程已**至少第三轮来信**：以**复述承接 + 多角度安抚**为主，**禁止**反复索要订单编号或凭证。

回复结构（顺序不可省略核心步骤）：
1. 共情、致歉并肯定对方关切；**禁止**填表式追问；**禁止**「本渠道无法处理退款」。涉及退款须移交 **after-sales**。
{info_step}
4. 语气：专业、温暖。

{as_block}
{compensation_instruction}
{escalation_note}"""
        elif intense_appeasement:
            style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。当前为**高冲突**场景：**优先**道歉与共情（**总长忌冗长**），再到索取沟通信息。

回复结构（顺序不可省略核心步骤）：
1. **简洁而充分**地道歉与共情，呼应不满点；**禁止** *I/we cannot process or authorize refunds through this email/channel* 等措辞；涉及退款须移交 **after-sales**。
{info_step}
4. 语气：友好、不把对方撂在一边。

{as_block}
{compensation_instruction}
{escalation_note}"""
        else:
            style_block = f"""你是 {config.BRAND_NAME} 品牌的高级客服专员。当前为**一般不满**场景：专业、温暖，**篇幅紧凑**。

回复结构（顺序不可省略核心步骤）：
1. **简短**共情、致歉与肯定关切（控制在**一至两段**）；**禁止** *If you could…*；**禁止** emphasis on channel无权退款；退款须移交 **after-sales**。**尽快**进入下方沟通要点。
{info_step}
4. 语气：友好、专业、忌重复致歉。

{as_block}
{compensation_instruction}
{escalation_note}"""

        extra_antiduplicate = (
            "- **多轮安抚专用**：**禁止**再次索要订单号/凭证（历史中已提及或可推断的内容只可肯定承接）；以其最新回复为依据做具体安抚，勿催补材料。"
            if calm_mode == "soothe_focus"
            else (
                "- **防重复**：来信或近期历史中已有订单号 → 不再次索要，仅感谢确认；已描述问题/不满 → 不再次「请具体说明/进一步描述」。"
                if calm_mode == "default"
                else (
                    "- **防重复**（首通三项）：(1)–(3) 任一项在来信或**近期历史**中**已给齐/已写清** → 只**致谢确认**，**禁止**再逐条要同一内容；可说明「已收悉将一并转交」。"
                )
            )
        )
        len_hint = (
            "**英文约 140–230 词**；多轮安抚仍以**简练**为重，禁用长串同义致歉。"
            if calm_mode == "soothe_focus"
            else (
                "**英文约 90–170 词**；一般不满**忌**冗长。"
                if calm_mode == "default"
                else "**首封正式回复**英文约 **100–190 词**；激烈场景约 **140–230 词**，仍忌堆砌。"
            )
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
            else (
                "客户已至少第三封来信：**禁止**反复索要订单信息；根据其最新表述多角度安抚，移交售后须符合【事实约束】。"
                if calm_mode == "soothe_focus"
                else "已有所述信息勿重复问；**仅缺订单号**时可明确索取订单号。"
            )
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
                max_tokens=780 if calm_mode == "soothe_focus" else 750,
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
            if calm_mode == "soothe_focus":
                if intense_appeasement:
                    open_so = (
                        "We’re truly sorry you’ve had to reach out again, and we hear how frustrating this has been. "
                        "Thank you for your patience and for explaining things—we’re taking your concerns seriously.\n\n"
                    )
                else:
                    open_so = (
                        "Thank you for continuing this conversation with us. We’re sorry this hasn’t been resolved yet, "
                        "and we appreciate the detail you’ve shared.\n\n"
                    )
                if after_sales_notified:
                    return (
                        f"Dear {contact_name},\n\n"
                        f"{open_so}"
                        f"We’ve logged everything from this thread and our after-sales team has been notified. "
                        f"You should hear back within about 1–2 business days. We won’t keep asking you here for the same order details you’ve already addressed.\n\n"
                        f"{config.BRAND_SIGNATURE}"
                    )
                return (
                    f"Dear {contact_name},\n\n"
                    f"{open_so}"
                    f"We’ve carefully noted your latest message and will hand this thread to our specialists so they can continue from here without repeating requests you’ve already covered.\n\n"
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
    product_name = _product_label(product, default="您的订单")
    history_lines = []
    for item in thread_history[-4:]:
        role = "客服" if item.get("is_mine") else contact_name
        history_lines.append(f"[{role}] {item.get('body', '')[:200].replace(chr(10), ' ')}")
    history_text = "\n".join(history_lines) or "（首次联系）"

    satisfied_extra = ""
    if sentiment == "satisfied":
        satisfied_extra = """
8. 【满意/致谢类来信专属】正文**最后一段之前**用**一两句（英文）**极委婉、可选的收尾（例如：if you ever leave a brief honest note where you purchased, it can help others decide—entirely optional, no pressure）。禁止命令式、禁止索要好评截图、禁止占过长篇幅。"""

    system_prompt = f"""你是 {config.BRAND_NAME} 品牌的客服专员。采用谦恭、专业的邮件风格。

回复要求：
1. {CUSTOMER_REPLY_ENGLISH_ANTISPAM}
{CUSTOMER_REPLY_REFUND_COMPENSATION_BOUNDARY}
2. 必须针对来信：感谢用户的反馈或直接回应需求；**肯定**对方表达的合理关切，必要时从**多个角度**简短安抚（重视、理解不便等）。但对于具体的产品操作、功能设置、技术指导等“How-to”问题，**绝对不要自行编造或猜测操作步骤**。应当礼貌地告知客户：已将该咨询转交至技术/产品团队，他们会尽快提供准确的操作指引。
3. 若来信涉及退款、赔偿、补偿金额或到账承诺：**绝不**在本信中代为答应、批准或否决；说明将由 **after-sales** 审核并专人跟进。**不要**写「本邮箱/本渠道无权处理退款」类句子。
4. 语气：温暖、专业；不夸大。
5. 长度：英文约 **70–140 词**。
6. 结尾署名：{config.BRAND_SIGNATURE}
7. 只输出回复正文。
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
    product_name = _product_label(product, default="未绑定产品")

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
