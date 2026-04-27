"""
llm_service.py — 大语言模型调用层

核心能力：
  1. call_llm()           通用 LLM HTTP 调用（兼容所有 OpenAI Chat Completions 格式服务商）
  2. detect_stage()       根据 Thread 历史判断当前处于哪个合作阶段（1-4）
  3. generate_kol_reply() 根据阶段 + 上下文生成合规、高情商的 KOL 回复邮件
"""

import json
import logging
import requests
from typing import Any

from app.config import config

logger = logging.getLogger(__name__)


# ─── 底层 LLM 调用（可替换任意服务商） ────────────────────────────────────────

def call_llm(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 1500
) -> str:
    """
    调用大语言模型 API（标准 OpenAI Chat Completions 格式）。

    更换服务商只需修改 .env 中的 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL，代码无需改动：
      - OpenAI GPT-4o:    LLM_BASE_URL=https://api.openai.com/v1
      - 通义千问 Plus:    LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
      - DeepSeek:         LLM_BASE_URL=https://api.deepseek.com/v1
      - 本地 Ollama:      LLM_BASE_URL=http://localhost:11434/v1

    Args:
        messages:    OpenAI 格式的对话消息列表 [{"role": ..., "content": ...}]
        temperature: 生成温度，判断类任务用低值（0.1-0.3），创作类用较高值（0.6-0.8）
        max_tokens:  最大生成 token 数

    Returns:
        str: 模型返回的文本内容

    Raises:
        Exception: 网络错误或 API 错误时抛出
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


# ─── 阶段判断 ──────────────────────────────────────────────────────────────────

def detect_stage(thread_history: list[dict], db_stage: int) -> dict:
    """
    让 LLM 分析 Thread 历史邮件，自动判断当前所处的合作阶段。

    Args:
        thread_history: 由 agent.py 格式化的消息列表，每条含 is_mine/subject/body 字段
        db_stage:       数据库中上次记录的阶段（作为 fallback）

    Returns:
        dict: {"stage": int(1-4), "reasoning": str}
    """
    # 构建供 LLM 阅读的对话摘要（每条消息截取前 400 字符，避免超长上下文）
    summary_lines = []
    for msg in thread_history:
        role = "【我方】" if msg.get("is_mine") else "【KOL】"
        body_snippet = msg["body"][:400].replace("\n", " ")
        summary_lines.append(f"{role} 主题: {msg['subject']}\n内容: {body_snippet}")

    history_text = "\n\n---\n\n".join(summary_lines) or "（无历史记录）"

    system_prompt = """你是一个 KOL 合作谈判进度分析专家。请根据以下邮件对话记录，判断当前合作所处阶段。

阶段定义：
- 阶段1 [破冰邀请]：我方刚发出初次邀请，或 KOL 尚未回复
- 阶段2 [规则确认]：KOL 表达了兴趣，正在了解/确认合作细节，或已确认合作意向

请只返回 JSON，格式：{"stage": <1|2>, "reasoning": "<简短中文判断理由>"}"""

    user_msg = f"数据库记录阶段：{db_stage}\n\n邮件历史（从旧到新）：\n\n{history_text}"

    try:
        raw = call_llm(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_msg}],
            temperature=0.1,
            max_tokens=200
        )
        # 清洗可能的 Markdown 代码块包裹
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(clean)
        stage = max(1, min(2, int(result.get("stage", db_stage))))
        return {"stage": stage, "reasoning": result.get("reasoning", "")}
    except Exception as e:
        logger.warning(f"⚠️ 阶段判断失败，沿用数据库阶段 {db_stage}: {e}")
        clamped = max(1, min(2, db_stage))
        return {"stage": clamped, "reasoning": "自动判断失败，使用上次记录"}


# ─── KOL 回复生成 ──────────────────────────────────────────────────────────────

# 每个阶段的具体任务说明，注入到 System Prompt 的任务区块
_STAGE_TASKS = {
    1: """
【当前任务：破冰邀请】
目标：以简洁真诚的商务语气表达合作意向，让对方愿意了解详情。

要点：
- 简短自我介绍品牌，一句话说明为何联系对方（如内容风格与品牌调性契合）
- 提出希望寄送产品供对方真实体验，不急于解释完整商业条件
- 全程像一封普通商务邮件，而非营销广告；不堆叠形容词和感叹
- 结尾用开放式问句邀请回复即可
""",
    2: """
【当前任务：规则确认】
目标：向已表现出兴趣的 KOL 清晰介绍合作方式，说明我方会提供免费产品寄送，由人工团队进一步对接合作细节。

要点：
- 感谢对方回信并表达合作期待
- 告知我们会安排免费产品寄送，后续由我方团队与其直接沟通合作细节
- 语气像普通商务往来邮件，简洁真诚，不堆叠溢美之词
- 请对方确认地址或最方便的联系方式，方便后续跟进
""",
}


def generate_kol_reply(
    thread_history: list[dict],
    kol_name: str,
    kol_email: str,
    stage: int,
    latest_message: str,
    candidate_products: list[dict] | None = None,
) -> str:
    """
    根据当前合作阶段和 Thread 历史，生成面向 KOL 的高情商回复邮件正文。

    核心特性：
      - 语言镜像：自动检测 KOL 最近来信的语言，用完全相同的语言回复
      - 极致服务语气：谦卑、热情、以对方为中心
      - 内容合规：不使用可能触发 Spam 或法律风险的词汇
      - 产品感知：若提供候选产品，由模型自主决定是否融入，最多提及 2 个

    Args:
        thread_history:     格式化后的历史消息列表（含多轮 KOL + 我方）
        kol_name:           KOL 姓名
        kol_email:          KOL 邮箱
        stage:              当前合作阶段 (1-4)
        latest_message:     KOL 最新一封来信的正文
        candidate_products: 可选候选产品列表，每条含 name/tagline/scene/intro 字段

    Returns:
        str: 可直接发送的回复邮件正文（纯文本）
    """
    # 格式化历史记录供 LLM 阅读（控制每条消息长度）
    history_lines = []
    for msg in thread_history:
        role = "Our Team" if msg.get("is_mine") else f"Creator ({kol_name or 'KOL'})"
        body_snippet = msg["body"][:config.BODY_EXCERPT_LENGTH].replace("\n", " ")
        history_lines.append(f"[{role}]: {body_snippet}")
    history_text = "\n\n---\n\n".join(history_lines) or "（首次联系，无历史记录）"

    stage_task = _STAGE_TASKS.get(max(1, min(2, stage)), _STAGE_TASKS[2])

    # 构建产品参考区块（仅在有候选产品时注入）
    product_section = ""
    if candidate_products:
        product_lines = []
        for p in candidate_products[:5]:
            line = f"- 【{p.get('name', '')}】{p.get('tagline', '')}（适用：{p.get('scene', '')}）"
            product_lines.append(line)
        product_section = f"""
# 可参考的品牌产品（供你自主决定是否提及）
以下是当前可供合作体验的产品，**你来判断**是否在这封邮件中自然提及——若提及，最多 2 个，融入邮件内容，切勿生硬推销：

{chr(10).join(product_lines)}

若不适合提及（如当前阶段不需要介绍产品），可完全忽略以上列表。
"""

    system_prompt = f"""你是 {config.BRAND_NAME} 品牌的高级 KOL 合作专员，也是一位极度专业、谦卑、热情的品牌大使。

# 人设与沟通风格
你对每一位内容创作者都怀有发自内心的尊重与欣赏。你的沟通风格：
- 像极其贴心的高级私人管家：谦卑、细腻、完全以对方为中心
- 把 KOL 当作独一无二的尊贵合作伙伴，无限放大对方的价值
- 用词温暖、真诚，绝不生硬或功利
- 称呼方式参考：Dear [名字]、尊敬的创作者朋友、了不起的 [名字] 等

# 核心约束（必须严格遵守）

1. 语言镜像（Language Mirroring）——最高优先级：
   检测 KOL 最近来信所用的语言，你的整封回复邮件必须使用完全相同的语言。
   对方英语 → 你用英语；日语 → 日语；西班牙语 → 西班牙语；以此类推。
   如果无法确定，默认使用英语。

2. 禁止词汇（绝对不出现）：
   fake review / paid review / buy reviews / 刷单 / 刷评 / 买好评
   以及任何可能被解读为"花钱购买虚假评价"的表述。

3. 推荐替代词汇：
   - Product Evaluation / 产品测评
   - cover your cost / refund your purchase / 报销购买费用（避免单独大写 REIMBURSEMENT）
   - Honest Sharing / Genuine Experience / 真诚分享 / 真实体验
   - Collaboration / Partnership / 合作 / 长期伙伴

4. 反垃圾邮件写作规范（Gmail 内容过滤器敏感点）：
   - 开头不超过 1 句问候，禁止连续 2 句以上的夸赞或溢美（会触发营销邮件检测）
   - 全文语气像普通商务往来邮件，而非广告文案
   - 避免全大写词汇、感叹号堆叠、以及"amazing / incredible / deeply inspired"此类营销腔
   - "reimburse / reimbursement" 每封最多出现 1 次，且用小写嵌入句子中
   - 避免开头连续 3 个词都是形容词或副词修饰语

5. 格式要求：
   - 直接输出邮件正文，不要加任何解释性文字或标注
   - 结尾署名使用：{config.BRAND_SIGNATURE}（不要用占位符）
   - 长度控制在 150-250 词，简洁自然，不要填充无实质内容的客套话
{product_section}
# 当前阶段任务
{stage_task}"""

    user_content = f"""KOL 信息：
- 姓名：{kol_name or '朋友'}
- 邮箱：{kol_email}

--- 邮件历史（从旧到新）---
{history_text}

--- KOL 最新来信 ---
{latest_message[:800]}

请根据以上背景，生成【阶段 {stage}】的回复邮件正文。"""

    return call_llm(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": user_content}],
        temperature=0.72,
        max_tokens=900
    )


def _clean_json_block(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[len("```json"):].strip()
    elif text.startswith("```"):
        text = text[len("```"):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def _keyword_score(text: str, product: dict) -> int:
    needle = (text or "").lower()
    score = 0
    for kw in product.get("keywords", []) or []:
        candidate = str(kw).strip().lower()
        if candidate and candidate in needle:
            score += 1
    return score


def recommend_products_for_creator(
    creator: dict,
    products: list[dict],
    top_n: int = 3,
) -> list[dict]:
    """
    根据达人画像做轻量产品推荐。
    首期优先用可解释的关键词匹配，避免把推荐完全交给黑盒。
    """
    if not products:
        return []

    profile_text = "\n".join([
        creator.get("name", ""),
        creator.get("platform", ""),
        creator.get("country", ""),
        creator.get("language", ""),
        " ".join(creator.get("tags", []) or []),
        creator.get("identity_summary", ""),
        creator.get("notes", ""),
    ])
    scored = sorted(
        ((product, _keyword_score(profile_text, product)) for product in products if product.get("is_active", True)),
        key=lambda item: item[1],
        reverse=True,
    )
    ranked = [item[0] for item in scored if item[1] > 0]
    if ranked:
        return ranked[:top_n]
    return [product for product in products if product.get("is_active", True)][:top_n]


def generate_outreach_email(
    creator: dict,
    product: dict,
    commission_rate: float,
    campaign_name: str | None = None,
) -> dict:
    """
    生成主动首封开发邮件草稿。
    返回: {"subject": str, "body": str}
    """
    creator_name = creator.get("name") or "there"
    product_name = product.get("name") or "our product"
    commission_text = f"{float(commission_rate or 0):g}%"
    tags_text = ", ".join(creator.get("tags", []) or []) or "content creator"

    # 只在佣金 > 0 时才提及，0% 写出来反而像诈骗信
    commission_hint = (
        f"- 如对合作有兴趣，可自然提到我方提供 {commission_text} 的销售分成，表述要口语化"
        if float(commission_rate or 0) > 0
        else "- 不要在邮件中提及任何佣金或分成数字"
    )

    system_prompt = f"""你是 {config.BRAND_NAME} 的达人合作开发专员。

请为首次主动联系达人生成一封自然、真诚、简洁的商务开发邮件草稿（纯文本，不含任何 HTML 标签）。

核心写作原则：
1. 默认使用英文，除非达人画像明显显示应使用其他语言。
2. 语气像朋友间的真实商务信，而非营销模板或群发广告。
3. 内容框架：
   - 第一句：简短提及你关注过对方的具体内容（不要泛泛称赞）
   - 说明产品与达人内容方向的自然契合点（1-2句）
   - 提出愿意寄产品给对方亲自试用（禁止用 "free sample"，改用 "send you a unit to try" 或类似自然表达）
   {commission_hint}
   - 结尾轻松邀请对方感兴趣时回复，不施加压力，不催促

【严格禁止，这些词会直接触发垃圾邮件过滤器】：
主题禁止出现：Collaboration Opportunity, Partnership Opportunity, Exciting, Elevate, Exclusive, Deal, Offer, Promotion, Amazing, Incredible
正文禁止出现：free sample, commission is set at, earn money, make money, click here, limited time, act now, guaranteed, no obligation, risk-free, 100%

其他格式禁止：
- 禁止使用多个感叹号（全文最多 1 个）
- 禁止全大写单词
- 禁止 HTML 标签（<p>, <br>, <a> 等一律不用）

主题行要求（非常重要）：
- 短且具体，最多 8-10 个词
- 像真人写给具体某位达人的私信，不是广告标题
- 好例子："Quick question about your home content"、"Your recent [topic] post — a thought"、"A product idea for your [platform] audience"
- 坏例子：任何含 Opportunity / Partnership / Elevate / Exciting 的标题

正文长度：120-180 词（不含署名）。
结尾署名：{config.BRAND_SIGNATURE}
仅返回 JSON：{{"subject":"...", "body":"..."}}"""

    user_prompt = f"""达人信息：
- 姓名：{creator_name}
- 邮箱：{creator.get('email', '')}
- 平台：{creator.get('platform', '')}
- 国家/语言：{creator.get('country', '')} / {creator.get('language', '')}
- 标签：{tags_text}
- 达人画像：{creator.get('identity_summary', '')}
- 备注：{creator.get('notes', '')}

产品信息：
- 名称：{product_name}
- 店铺名：{product.get('store_name', config.BRAND_NAME)}
- ASIN：{product.get('asin', '')}
- 卖点：{product.get('tagline', '')}
- 场景：{product.get('scene', '')}
- 描述：{product.get('description') or product.get('intro', '')}
- 关键词：{", ".join(product.get('keywords', []) or [])}

批次信息：
- 名称：{campaign_name or 'default outreach'}

请输出首封开发邮件草稿。"""

    try:
        raw = call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.55,
            max_tokens=700,
        )
        data = json.loads(_clean_json_block(raw))
        subject = (data.get("subject") or "").strip()
        body = (data.get("body") or "").strip()
        if subject and body:
            return {"subject": subject, "body": body}
    except Exception as exc:
        logger.warning(f"⚠️ 首封开发邮件生成失败，使用兜底模板: {exc}")

    platform = creator.get('platform') or 'your channel'
    tagline = product.get('tagline') or product.get('scene') or 'everyday use'
    subject = f"A quick question about your {platform} content"
    commission_line = (
        f"If it's a good fit, we can also discuss a {commission_text} revenue share for future posts.\n\n"
        if float(commission_rate or 0) > 0
        else ""
    )
    body = (
        f"Hi {creator_name},\n\n"
        f"I came across your {platform} content and really liked how you cover {tags_text} — "
        f"it aligns closely with what {product_name} is designed for.\n\n"
        f"We'd love to send you a unit to try. {product_name} is built around {tagline}, "
        f"and I think it could be a natural fit for the kind of content your audience enjoys.\n\n"
        f"{commission_line}"
        f"Would you be open to hearing more? Happy to share details whenever it's convenient.\n\n"
        f"{config.BRAND_SIGNATURE}"
    )
    return {"subject": subject, "body": body}


def detect_creator_reply_intent(
    creator: dict,
    latest_message: str,
    thread_history: list[dict],
    product: dict | None = None,
) -> dict:
    """
    识别达人回信意图，返回结构化结果。
    intent:
      - interested
      - not_interested
      - need_followup
      - manual_review
    """
    creator_name = creator.get("name") or creator.get("email") or "creator"
    product_name = product.get("name") if product else ""
    history_lines = []
    for item in thread_history[-6:]:
        role = "Our Team" if item.get("is_mine") else creator_name
        history_lines.append(f"[{role}] {item.get('body', '')[:260].replace(chr(10), ' ')}")
    history_text = "\n".join(history_lines) or "(no history)"

    system_prompt = """你是达人商务回信分类助手。

我方已向达人发送产品合作邀请邮件，现在需要判断达人回信的意图。

请根据回信内容判断意图，并只返回 JSON：
{
  "intent": "interested|not_interested|need_followup|manual_review",
  "confidence": 0-1,
  "summary": "一句中文摘要",
  "reasoning": "一句简短原因"
}

判定标准（前两类均会生成合作工单由人工跟进）：
- interested: 达人明确表达合作意向——愿意试用产品、愿意合作、愿意了解/推进细节、表示可以沟通
- need_followup: 达人有正面/积极态度，但提出了疑问或需要更多信息（如问产品细节、佣金比例、合作流程等）——这也是有意向的信号，同样生成工单
- not_interested: 达人明确拒绝、婉拒、表示暂不感兴趣或不接受此类合作
- manual_review: 回信语义完全模糊、与合作无关、或系统无法稳妥判断（如自动回复、乱码、纯问候等）"""

    user_prompt = f"""达人：{creator_name}
产品：{product_name}

近期历史：
{history_text}

最新回信：
{latest_message[:1500]}"""

    try:
        raw = call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=220,
        )
        result = json.loads(_clean_json_block(raw))
        intent = result.get("intent", "manual_review")
        if intent not in {"interested", "not_interested", "need_followup", "manual_review"}:
            intent = "manual_review"
        return {
            "intent": intent,
            "confidence": float(result.get("confidence", 0) or 0),
            "summary": result.get("summary", "") or result.get("reasoning", ""),
            "reasoning": result.get("reasoning", ""),
            "raw": _clean_json_block(raw),
        }
    except Exception as exc:
        logger.warning(f"⚠️ 意图识别失败，使用规则兜底: {exc}")

    text = (latest_message or "").lower()
    if any(token in text for token in ["not interested", "no thanks", "pass", "decline", "unsubscribe", "不感兴趣", "暂不", "不用了", "拒绝", "不需要"]):
        return {
            "intent": "not_interested",
            "confidence": 0.72,
            "summary": "达人明确表示当前不考虑合作。",
            "reasoning": "规则兜底命中拒绝词。",
            "raw": "",
        }
    if any(token in text for token in ["interested", "sounds good", "let's do it", "yes", "sure", "absolutely", "love to", "happy to", "合作", "感兴趣", "可以", "了解一下", "愿意"]):
        return {
            "intent": "interested",
            "confidence": 0.55,
            "summary": "达人表达了合作意向。",
            "reasoning": "规则兜底命中明确合作积极词。",
            "raw": "",
        }
    if any(token in text for token in ["how", "details", "commission", "rate", "what product", "shipping", "more info", "tell me", "流程", "佣金", "产品", "细节", "怎么", "什么条件", "如何合作"]):
        return {
            "intent": "need_followup",
            "confidence": 0.61,
            "summary": "达人有意向并询问合作细节，需人工跟进。",
            "reasoning": "规则兜底命中追问词，判定为有意向待跟进。",
            "raw": "",
        }
    return {
        "intent": "manual_review",
        "confidence": 0.3,
        "summary": "回信语义不够明确，仅记录。",
        "reasoning": "规则兜底未命中明确意图。",
        "raw": "",
    }


def generate_polite_decline_reply(
    creator: dict,
    product: dict | None = None,
) -> str:
    creator_name = creator.get("name") or "there"
    product_name = product.get("name") if product else "our products"

    system_prompt = f"""你是 {config.BRAND_NAME} 的达人合作专员。
请写一封非常简短、真诚、有分寸的感谢回复邮件，适用于达人婉拒合作的场景。

要求：
- 默认使用英文，除非达人资料明显显示应使用其他语言
- 感谢对方回复
- 表达未来如果对 {product_name} 或其他新品有兴趣，欢迎随时联系
- 不要施压，不要再次推销
- 控制在 60-120 词
- 结尾署名使用：{config.BRAND_SIGNATURE}
- 只输出正文"""

    user_prompt = f"""达人姓名：{creator_name}
达人语言：{creator.get('language', '')}
产品：{product_name}"""

    try:
        return call_llm(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=220,
        )
    except Exception as exc:
        logger.warning(f"⚠️ 感谢回复生成失败，使用模板兜底: {exc}")
        return (
            f"Hi {creator_name},\n\n"
            f"Thank you for getting back to us. We completely understand, and we appreciate you taking the time to reply. "
            f"If you ever feel that {product_name} or any future launches might be a fit for your audience, "
            f"please feel free to reach out anytime.\n\n"
            f"{config.BRAND_SIGNATURE}"
        )
