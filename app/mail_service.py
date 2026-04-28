"""
mail_service.py — 阿里企业邮箱 IMAP 收件 + SMTP 发件（客服场景）

功能：
  1. fetch_unread_emails()        IMAP 拉取未读来信
  2. send_reply()                 回复用户邮件（可 Cc 负责人；Thread 串联；MIME 对齐阿里网页投递画像）
  3. send_internal_escalation()   内部升级邮件（含原文+中译；按同线程推送次序标注「初次推送 / 第二次推送 / …」）

对外回复 MIME 策略（对齐阿里邮箱网页 Ding/Web 成功样本）：
  - boundary：----=ALIBOUNDARY_*（非伪造 Outlook）
  - parts：UTF-8 + base64（与网页一致）
  - Message-ID：<uuid.support@域名>
  - 不设虚假的 X-Mailer；网页同源 Reply-To
  - In-Reply-To + References 串联 Thread
"""

import logging
import re
import smtplib
import ssl
import html as html_lib
import uuid
from email import charset as _charset_mod
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header as _decode_header_lib
from email.utils import parseaddr, formataddr, formatdate

# 阿里邮箱网页 multipart 常用 UTF-8 + base64 body（与用户手工成功信一致）。
_UTF8_B64 = _charset_mod.Charset("utf-8")
_UTF8_B64.body_encoding = _charset_mod.BASE64


def _aliyun_web_boundary() -> str:
    """对齐阿里邮箱网页 multipart boundary：----=ALIBOUNDARY_<n>_<hex>_<suffix>"""
    n = uuid.uuid4().int % 9000 + 1000
    mid = uuid.uuid4().hex[:12]
    tail = uuid.uuid4().hex[:5]
    return f"----=ALIBOUNDARY_{n}_{mid}_{tail}"


def _web_like_message_id(domain: str) -> str:
    """对齐网页端 Message-ID 形态：<uuid.support@domain>"""
    return f"<{uuid.uuid4()}.support@{domain}>"

from imap_tools import MailBox, AND

from app.config import config

_SUBJECT_REPLY_PREFIX = re.compile(r"^(?:re\s*:\s*|回复\s*[:：]\s*)+", re.IGNORECASE)


def _strip_reply_subject_prefixes(subject: str) -> str:
    s = (subject or "").strip()
    while True:
        m = _SUBJECT_REPLY_PREFIX.match(s)
        if not m:
            break
        s = s[m.end() :].strip()
    return s


def _reply_subject_for_send(original_subject: str) -> str:
    """构造回复主题：可选阿里网页同款「回复：」前缀（config.MAIL_REPLY_SUBJECT_WEB_STYLE）。"""
    subj = original_subject or ""
    if getattr(config, "MAIL_REPLY_SUBJECT_WEB_STYLE", True):
        core = _strip_reply_subject_prefixes(subj)
        return f"回复：{core}" if core else "回复："
    low = subj.lower()
    if low.startswith("re:"):
        return subj
    return f"Re: {subj}" if subj.strip() else "Re:"


logger = logging.getLogger(__name__)


# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def decode_str(value: str) -> str:
    """
    解码 RFC 2047 编码的邮件头字段。
    例：=?UTF-8?B?5L2g5aW9?= → 你好
    """
    if not value:
        return ""
    parts = _decode_header_lib(value)
    result = ""
    for raw, charset in parts:
        if isinstance(raw, bytes):
            result += raw.decode(charset or "utf-8", errors="replace")
        else:
            result += raw
    return result.strip()


def parse_sender(from_header: str) -> tuple[str, str]:
    """
    解析发件人姓名和邮箱。
    "John Doe <john@example.com>" → ("John Doe", "john@example.com")
    """
    name, addr = parseaddr(decode_str(from_header))
    return name.strip(), addr.strip()


# ─── IMAP 收件 ─────────────────────────────────────────────────────────────────

def fetch_unread_emails(limit: int = 20) -> list[dict]:
    """
    通过 IMAP 获取收件箱未读邮件，返回结构化字典列表。

    每条字典包含：
      uid, message_id(RFC-2822), thread_message_ids(References链),
      subject, from_name, from_email, body, date
    """
    results = []
    try:
        logger.info(f"📡 连接 IMAP: {config.IMAP_HOST}:{config.IMAP_PORT}")
        with MailBox(config.IMAP_HOST, config.IMAP_PORT).login(
            config.EMAIL_ADDRESS, config.EMAIL_PASSWORD
        ) as mb:
            logger.info("✅ IMAP 登录成功")

            # 搜索未读邮件，按时间正序取最新的 limit 封
            msgs = list(mb.fetch(AND(seen=False), limit=limit, reverse=True))
            logger.info(f"📬 发现 {len(msgs)} 封未读邮件")

            for msg in msgs:
                # imap-tools 1.5.0: msg.headers 是 Dict[str, List[str]]
                # 用安全的辅助函数取单个值
                def _h(key: str) -> str:
                    vals = msg.headers.get(key) or []
                    return vals[0].strip() if vals else ""

                from_raw = _h("from")
                from_name, from_email = parse_sender(from_raw)

                # RFC 2822 Message-ID（防 Spam 关键字段）
                raw_msg_id = _h("message-id")

                # References 链（用于串联整个 Thread）
                references = _h("references")

                body = msg.text or msg.html or ""

                results.append({
                    "uid":        str(msg.uid),
                    "message_id": raw_msg_id,
                    "references": references,
                    "subject":    decode_str(msg.subject or ""),
                    "from_name":  from_name,
                    "from_email": from_email,
                    "from_raw":   from_raw,
                    "body":       body.strip(),
                    "date":       str(msg.date),
                })

    except Exception as e:
        logger.error(f"❌ IMAP 收件失败: {e}", exc_info=True)

    return results


# ─── SMTP 发件（含防 Spam 头部） ───────────────────────────────────────────────

def send_reply(
    original: dict,
    reply_body: str,
    cc_emails: list[str] | None = None,
) -> bool:
    """
    通过阿里企业邮箱 SMTP 发送回复邮件。

    MIME 对齐网页 Ding/Web：boundary ALIBOUNDARY_*、UTF-8 base64 正文、
    Message-ID（uuid.support@域名）、Reply-To 同源；不设虚假 X-Mailer。

    Thread 串联：
    ┌─────────────────────────────────────────────────────────────────┐
    │  In-Reply-To: <原邮件 Message-ID>                               │
    │  References:  <原邮件 References 链> <原邮件 Message-ID>        │
    └─────────────────────────────────────────────────────────────────┘

    Args:
        original:   由 fetch_unread_emails() 返回的原邮件字典
        reply_body: 纯文本回复正文
        cc_emails:  抄送地址列表（如产品负责人）

    Returns:
        bool: 发送成功返回 True
    """
    try:
        to_email = original["from_email"]

        subject = original["subject"]
        reply_subject = _reply_subject_for_send(subject)

        # ── 构建 References 链 ─────────────────────────────────────────────
        orig_msg_id    = original["message_id"]
        orig_refs      = original.get("references", "").strip()
        new_references = f"{orig_refs} {orig_msg_id}".strip() if orig_refs else orig_msg_id

        mail_domain = config.EMAIL_ADDRESS.split("@")[-1]

        # ── multipart/alternative：boundary / base64 / HTML 片段对齐阿里邮箱网页 ──
        msg = MIMEMultipart("alternative", boundary=_aliyun_web_boundary())

        from_hdr = formataddr((config.SENDER_DISPLAY_NAME, config.EMAIL_ADDRESS))
        msg["From"] = from_hdr
        # 网页端同源 Reply-To（成功样本含此头；勿伪造 Outlook）
        msg["Reply-To"] = from_hdr

        # To: 保留原始收件人格式（含显示名）
        msg["To"] = original["from_raw"] or to_email

        cc_list = [e.strip() for e in (cc_emails or []) if e and str(e).strip()]
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)

        msg["Subject"]    = reply_subject
        msg["Date"]       = formatdate(localtime=True)
        msg["Message-ID"] = _web_like_message_id(mail_domain)

        # ── Thread 串联头部 ──────────────────────────────────────────────────
        msg["In-Reply-To"] = orig_msg_id
        msg["References"]  = new_references

        # 纯文本先行，HTML 后附（RFC 2046）；编码 UTF-8 base64 对齐网页 Ding/Web
        text_part = MIMEText(reply_body, "plain", _UTF8_B64)
        msg.attach(text_part)

        html_body = _text_to_html_aliyun_web(reply_body)
        html_part = MIMEText(html_body, "html", _UTF8_B64)
        msg.attach(html_part)

        # ── SSL 连接并发送 ─────────────────────────────────────────────────
        log_cc = f" | Cc: {cc_list}" if cc_list else ""
        logger.info(f"📤 发送回复 → {to_email}{log_cc} | 主题: {reply_subject}")
        logger.info(f"   In-Reply-To: {orig_msg_id[:60]}")
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=context) as server:
            server.login(config.EMAIL_ADDRESS, config.EMAIL_PASSWORD)
            server.send_message(msg)

        logger.info(f"✅ 发送成功 → {to_email}")
        return True

    except Exception as e:
        logger.error(f"❌ SMTP 发送失败: {e}", exc_info=True)
        return False


def _escalation_push_label(push_sequence: int) -> str:
    """同线程内部升级邮件的主题/正文标签：1=初次推送，2=第二次推送，其余=第N次推送。"""
    n = max(1, int(push_sequence))
    if n == 1:
        return "初次推送"
    if n == 2:
        return "第二次推送"
    return f"第{n}次推送"


def send_internal_escalation(
    *,
    to_email: str,
    to_name: str,
    thread_id: str,
    contact_name: str,
    contact_email: str,
    product_name: str,
    priority: str,
    escalation_summary: str,
    original_message: str = "",
    original_message_zh: str = "",
    push_sequence: int = 1,
) -> bool:
    """
    向产品负责人发送内部客服升级通知邮件。

    Args:
        to_email:              收件人邮箱（产品 owner_email 或 DEFAULT_SUPPORT_OWNER_EMAIL）
        to_name:               收件人姓名
        thread_id:             会话 ID（邮件线程 key，排障用）
        contact_name:          触发升级的联系人姓名
        contact_email:         触发升级的联系人邮箱
        product_name:          绑定产品名称
        priority:              优先级建议（如 high / medium）
        escalation_summary:    由 LLM 生成的 ≤5 行摘要
        original_message:      用户来信原文（任意语种，节选）
        original_message_zh:   原文的简体中文完整译文，供内部客服阅读
        push_sequence:         本会话内向客服的第几次推送（1=初次，2=第二次，依此类推）

    Returns:
        bool: 发送成功返回 True
    """
    try:
        seq = max(1, int(push_sequence))
        phase_label = _escalation_push_label(seq)
        subject = f"[客服升级·{phase_label}] {contact_name} — {product_name}"
        orig = (original_message or "").strip()
        orig_zh = (original_message_zh or "").strip()
        if orig and orig_zh:
            original_block = (
                f"【用户原文】\n{orig}\n\n"
                f"【中文译文】（供内部阅读）\n{orig_zh}\n"
            )
        elif orig:
            original_block = (
                f"【用户原文】\n{orig}\n\n"
                f"【中文译文】\n"
                f"（系统暂未能自动生成译文，请根据上方原文处理。）\n"
            )
        else:
            original_block = ""

        if seq == 1:
            phase_intro = (
                f"【推送次序】{phase_label}（本会话第 1 封内部同步）\n"
                "说明：本会话首次因客诉/不满等向客服推送，便于尽早知晓并建档；"
                "若客户尚未提供订单号等，后续来信可能还会收到「第二次推送」及后续同步。\n\n"
            )
        elif seq == 2:
            phase_intro = (
                f"【推送次序】{phase_label}（本会话第 2 封内部同步）\n"
                "说明：本会话已向客服做过初次推送；本封为客户再次来信后的跟进同步，"
                "便于掌握最新补充内容（如订单号、新描述等），请在原工单或会话基础上继续处理。\n\n"
            )
        else:
            phase_intro = (
                f"【推送次序】{phase_label}（本会话第 {seq} 封内部同步）\n"
                "说明：同会话的再次升级同步，请结合历史推送与最新摘要继续跟进。\n\n"
            )

        body = (
            f"{phase_intro}"
            f"【客服升级通知】\n\n"
            f"以下工单需要您的关注：\n\n"
            f"{escalation_summary}\n\n"
        )
        if original_block:
            body += f"---\n{original_block}\n---\n"
        closing = (
            "请在本工单/会话基础上继续跟进处理。"
            if seq >= 2
            else "请尽快与该联系人跟进处理。"
        )
        body += (
            f"会话 ID：{thread_id}\n"
            f"联系人邮箱：{contact_email}\n"
            f"优先级建议：{priority}\n"
            f"品牌：{config.BRAND_NAME}\n\n"
            f"{closing}\n\n"
            f"{config.BRAND_SIGNATURE}"
        )

        md = config.EMAIL_ADDRESS.split("@")[-1]
        msg = MIMEMultipart("alternative", boundary=_aliyun_web_boundary())
        msg["From"] = formataddr((config.SENDER_DISPLAY_NAME, config.EMAIL_ADDRESS))
        msg["To"] = formataddr((to_name or to_email, to_email))
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = _web_like_message_id(md)

        text_part = MIMEText(body, "plain", _UTF8_B64)
        html_part = MIMEText(_text_to_html_aliyun_web(body), "html", _UTF8_B64)
        msg.attach(text_part)
        msg.attach(html_part)

        logger.info(f"📤 发送内部升级通知（{phase_label}）→ {to_email} | 主题: {subject}")
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=context) as server:
            server.login(config.EMAIL_ADDRESS, config.EMAIL_PASSWORD)
            server.send_message(msg)

        logger.info(f"✅ 内部升级通知发送成功 → {to_email}")
        return True

    except Exception as exc:
        logger.error(f"❌ 内部升级通知发送失败: {exc}", exc_info=True)
        return False


# ─── HTML 生成辅助（对齐阿里邮箱网页 Ding/Web 正文片段，避免完整 HTML 文档 + 伪造客户端指纹） ─

def _strip_html_tags(text: str) -> str:
    """移除 LLM 偶尔在纯文本正文里混入的 HTML 标签（<p>, <br>, <a> 等）。"""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s{3,}", "\n\n", text)
    return text.strip()


def _text_to_html_aliyun_web(text: str) -> str:
    """
    将纯文本转为阿里邮箱网页常见的 div/p 片段（无外层的完整 HTML 文档），
    与 multipart/alternative + base64 组合形态更接近手动网页回信。
    """
    clean_text = _strip_html_tags(text)
    escaped = html_lib.escape(clean_text)
    paragraphs = escaped.split("\n\n")
    chunks: list[str] = []
    for para in paragraphs:
        lines = para.strip()
        if not lines:
            continue
        if lines.startswith("---"):
            footer_content = lines[3:].strip()
            chunks.append(
                '<p style="font-size:11px;color:rgb(136,136,136);border-top:1px solid rgb(229,229,229);'
                'padding-top:12px;margin-top:24px;font-family:Arial,Helvetica,sans-serif;">'
                f'{footer_content.replace(chr(10), "<br>")}</p>'
            )
        else:
            chunks.append(
                '<p style="color:rgb(34,34,34);font-family:Arial,Helvetica,sans-serif;'
                'font-size:14px;line-height:1.6;margin:0 0 1em 0;">'
                f'{lines.replace(chr(10), "<br>")}</p>'
            )
    inner = "".join(chunks)
    return (
        '<div class="__aliyun_email_body_block">'
        '<div style="clear:both;font-family:Tahoma,Arial,STHeitiSC-Light,SimSun;font-size:14px;">'
        f"{inner}"
        "</div></div>"
    )


def _text_to_html(text: str, sender_name: str = "") -> str:
    """兼容旧调用；sender_name 保留不参与渲染。"""
    del sender_name
    return _text_to_html_aliyun_web(text)
