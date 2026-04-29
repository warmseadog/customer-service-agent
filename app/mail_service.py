"""
mail_service.py — 阿里企业邮箱 IMAP 收件 + SMTP 发件（客服场景）

功能：
  1. fetch_unread_emails()        IMAP 拉取未读来信
  2. send_reply()                 回复用户邮件（可 Cc 负责人；Thread 串联；MIME 对齐阿里网页投递画像）
  3. send_internal_escalation()   内部升级邮件（前半简要摘要与索引；后半来信原文 + 中译）

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


def _reply_subject_for_send(original_subject: str, *, mail_reply_subject_web_style: bool = True) -> str:
    subj = original_subject or ""
    if mail_reply_subject_web_style:
        core = _strip_reply_subject_prefixes(subj)
        return "回复：" + core if core else "回复："
    low = subj.lower()
    if low.startswith("re:"):
        return subj
    tail = subj.strip()
    return "Re: " + tail if tail else "Re:"
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

def fetch_unread_emails(mailbox_row: dict, limit: int = 20) -> list[dict]:
    results: list = []
    try:
        host = (mailbox_row.get('imap_host') or '').strip()
        port = int(mailbox_row.get('imap_port') or 993)
        email_addr = (mailbox_row.get('email_address') or '').strip()
        password = mailbox_row.get('password') or ''
        mid = int(mailbox_row.get('id') or 0)
        logger.info(f"IMAP {host}:{port} {email_addr}")
        with MailBox(host, port).login(email_addr, password) as mb:
            msgs = list(mb.fetch(AND(seen=False), limit=limit, reverse=True))
            logger.info(f"Unread {len(msgs)}")
            for msg in msgs:

                def _h(key: str) -> str:
                    vals = msg.headers.get(key) or []
                    return vals[0].strip() if vals else ''

                from_raw = _h("from")
                from_name, from_email = parse_sender(from_raw)
                raw_msg_id = _h("message-id")
                references = _h("references")
                body = msg.text or msg.html or ''

                results.append({
                    "uid": str(msg.uid),
                    "message_id": raw_msg_id,
                    "references": references,
                    "subject": decode_str(msg.subject or ""),
                    "from_name": from_name,
                    "from_email": from_email,
                    "from_raw": from_raw,
                    "body": body.strip(),
                    "date": str(msg.date),
                    "mailbox_id": mid,
                })

    except Exception as e:
        logger.error(f"IMAP fetch failed: {e}", exc_info=True)

    return results

def send_reply(
    mailbox_row: dict,
    original: dict,
    reply_body: str,
    cc_emails: list[str] | None = None,
) -> bool:
    try:
        to_email = original['from_email']
        subject = original['subject']
        web_style = bool(mailbox_row.get('mail_reply_subject_web_style', 1))
        reply_subject = _reply_subject_for_send(subject, mail_reply_subject_web_style=web_style)
        orig_msg_id = original['message_id']
        orig_refs = (original.get('references') or '').strip()
        new_references = f'{orig_refs} {orig_msg_id}'.strip() if orig_refs else orig_msg_id
        email_acc = (mailbox_row.get('email_address') or '').strip()
        mail_domain = email_acc.split('@')[-1] if '@' in email_acc else 'localhost'
        snd = (mailbox_row.get('sender_display_name') or 'Support').strip()
        msg = MIMEMultipart('alternative', boundary=_aliyun_web_boundary())
        from_hdr = formataddr((snd, email_acc))
        msg['From'] = from_hdr
        msg['Reply-To'] = from_hdr
        msg['To'] = original.get('from_raw') or to_email
        cc_list = [e.strip() for e in (cc_emails or []) if e and str(e).strip()]
        if cc_list:
            msg['Cc'] = ', '.join(cc_list)
        msg['Subject'] = reply_subject
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = _web_like_message_id(mail_domain)
        msg['In-Reply-To'] = orig_msg_id
        msg['References'] = new_references
        text_part = MIMEText(reply_body, 'plain', _UTF8_B64)
        msg.attach(text_part)
        html_part = MIMEText(_text_to_html_aliyun_web(reply_body), 'html', _UTF8_B64)
        msg.attach(html_part)
        log_cc = f' | Cc: {cc_list}' if cc_list else ''
        logger.info(f'send reply -> {to_email}{log_cc} subj={reply_subject}')
        _smtp_send_message(mailbox_row, msg)
        logger.info('sent ok -> %s', to_email)
        return True
    except Exception as e:
        logger.error('SMTP send_reply failed: %s', e, exc_info=True)
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
    mailbox_row: dict,
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
    向产品负责人发送内部升级通知邮件。

    正文结构：前半为简短推送标签 + LLM 摘要 + 极简索引行；后半为来信原文与中文译文，末尾一行跟进提示。

    Args:
        to_email:              收件人邮箱（产品 owner_email 或 DEFAULT_SUPPORT_OWNER_EMAIL）
        to_name:               收件人姓名
        thread_id:             会话 ID（邮件线程 key，排障用）
        contact_name:          触发升级的联系人姓名（用于主题行）
        contact_email:         触发升级的联系人邮箱
        product_name:          绑定产品名称（用于主题行）
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

        # 前半：推送标签 + 摘要 + 极简索引；后半：来信原文与译文（不加冗长说明）
        phase_one_liner = (
            f"【推送】{phase_label} · 本会话第 {seq} 封"
            + (" · 客户再次来信跟进" if seq >= 2 else " · 首次同步")
            + "\n\n"
        )

        brand_disp = (mailbox_row.get("brand_name") or "").strip() or config.BRAND_NAME
        # 索引行尽量短（摘要里已有联系人/产品等）；后半仍为全文原文 + 译文
        brief_meta = (
            f"会话 ID：{thread_id}\n"
            f"联系人邮箱：{contact_email}\n"
            f"优先级：{priority} · 品牌：{brand_disp}\n\n"
        )

        body = (
            f"{phase_one_liner}"
            f"【摘要】\n"
            f"{escalation_summary.strip()}\n\n"
            f"{brief_meta}"
            f"————————————————————\n"
            f"以下为来信原文与中文译文\n"
            f"————————————————————\n\n"
        )
        if original_block:
            body += original_block.rstrip() + "\n\n"
        else:
            body += "（本封未附带原文节选，请以摘要与会话 ID 排查后台或邮箱线程。）\n\n"
        closing = (
            "请结合摘要与全文在原会话或工单上继续处理。"
            if seq >= 2
            else "请尽快跟进。"
        )
        body += (
            f"————————————————————\n"
            f"{closing}\n\n"
            f"{(mailbox_row.get('brand_signature') or '').strip() or config.BRAND_SIGNATURE}"
        )

        email_acc = (mailbox_row.get('email_address') or '').strip()
        md = email_acc.split('@')[-1] if '@' in email_acc else 'localhost'
        msg = MIMEMultipart("alternative", boundary=_aliyun_web_boundary())
        snd = (mailbox_row.get('sender_display_name') or config.SENDER_DISPLAY_NAME).strip()
        msg['From'] = formataddr((snd, email_acc))
        msg["To"] = formataddr((to_name or to_email, to_email))
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = _web_like_message_id(md)

        text_part = MIMEText(body, "plain", _UTF8_B64)
        html_part = MIMEText(_text_to_html_aliyun_web(body), "html", _UTF8_B64)
        msg.attach(text_part)
        msg.attach(html_part)

        logger.info("internal escalation %s -> %s", phase_label, to_email)
        _smtp_send_message(mailbox_row, msg)
        logger.info("internal escalation sent -> %s", to_email)
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

def _smtp_send_message(mailbox_row: dict, msg: MIMEMultipart) -> None:
    ctx = ssl.create_default_context()
    host = (mailbox_row.get('smtp_host') or '').strip()
    port = int(mailbox_row.get('smtp_port') or 465)
    user = (mailbox_row.get('email_address') or '').strip()
    pw = mailbox_row.get('password') or ''
    use_ssl = bool(mailbox_row.get('smtp_use_ssl', 1))
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ctx) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)


