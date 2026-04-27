"""
mail_service.py — 阿里企业邮箱 IMAP 收件 + SMTP 发件

防 Spam 核心设计（解决 Gmail 将回复判为垃圾邮件的根本原因）：
─────────────────────────────────────────────────────────────
Gmail 将邮件判为 Spam 通常有以下原因：
  A. 内容触发词        → 由 LLM 层保证不出现高危词汇
  B. 无法识别为合法回复 → 通过 In-Reply-To + References 头部解决
  C. 邮件格式可疑       → 通过 multipart/alternative + 真实头部解决

关键设计：
  1. In-Reply-To + References：串联 Thread，Gmail 识别为合法会话回复
  2. multipart/alternative：同时发送 HTML + 纯文本，与真实商务邮件格式一致
  3. Auto-Submitted: no：告知 Gmail 这是人工发送的回复，非批量自动化邮件
  4. X-Mailer: Microsoft Outlook 16.0：消除 Python email 库的自动化指纹
─────────────────────────────────────────────────────────────
"""

import logging
import smtplib
import ssl
import html as html_lib
import uuid
from datetime import datetime
from email import charset as _charset_mod
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header as _decode_header_lib
from email.utils import parseaddr, make_msgid, formataddr, formatdate

# quoted-printable 比 base64 更接近真实邮件客户端（Outlook/Gmail）的编码方式，
# 能降低被 Gmail 内容过滤器识别为批量自动化邮件的概率。
_UTF8_QP = _charset_mod.Charset("utf-8")
_UTF8_QP.body_encoding = _charset_mod.QP


def _outlook_boundary() -> str:
    """
    生成与 Microsoft Outlook 格式相同的 MIME boundary。
    Python email 库默认生成 '===============...==' 格式，
    这是自动化脚本的显著指纹，会被反垃圾引擎直接识别。
    Outlook 格式示例：----=_Part_a3f2b1c4_1745720012345
    """
    rand_hex = uuid.uuid4().hex[:8]
    timestamp_ms = int(datetime.now().timestamp() * 1000)
    return f"----=_Part_{rand_hex}_{timestamp_ms}"

from imap_tools import MailBox, AND

from app.config import config

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
) -> bool:
    """
    通过阿里企业邮箱 SMTP 发送回复邮件。

    防 Spam 关键操作（相比旧代码的核心改进）：
    ┌─────────────────────────────────────────────────────────────────┐
    │  In-Reply-To: <原邮件 Message-ID>                               │
    │  References:  <原邮件 References 链> <原邮件 Message-ID>        │
    │                                                                 │
    │  这两个头部告诉 Gmail："这封邮件是对某个已知对话的合法回复"，    │
    │  而不是一封陌生的外发邮件，从而绕过垃圾邮件过滤器。             │
    └─────────────────────────────────────────────────────────────────┘

    Args:
        original:   由 fetch_unread_emails() 返回的原邮件字典
        reply_body: 纯文本回复正文

    Returns:
        bool: 发送成功返回 True
    """
    try:
        to_email = original["from_email"]

        # 构造 Re: 主题（避免重复添加 Re:）
        subject = original["subject"]
        reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

        # ── 构建 References 链 ─────────────────────────────────────────────
        orig_msg_id    = original["message_id"]
        orig_refs      = original.get("references", "").strip()
        new_references = f"{orig_refs} {orig_msg_id}".strip() if orig_refs else orig_msg_id

        # ── 构造 multipart/alternative（HTML + 纯文本双版本） ──────────────
        msg = MIMEMultipart("alternative", boundary=_outlook_boundary())

        # From: 真实人名 + 邮箱（比单独邮箱地址可信度更高，不像机器发送）
        msg["From"] = formataddr((config.SENDER_DISPLAY_NAME, config.EMAIL_ADDRESS))

        # To: 保留原始收件人格式（含显示名）
        msg["To"] = original["from_raw"] or to_email

        msg["Subject"]    = reply_subject
        msg["Date"]       = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=config.EMAIL_ADDRESS.split("@")[-1])
        msg["X-Mailer"]   = "Microsoft Outlook 16.0"

        # ── Thread 串联头部（告知 Gmail 这是合法会话回复） ────────────────
        msg["In-Reply-To"] = orig_msg_id
        msg["References"]  = new_references

        # 注意：以下头部刻意省略，原因如下：
        #   Reply-To   — 与 From 相同时是冗余且可疑的，真人 Gmail/Outlook 回复不带此头
        #   Auto-Submitted — 加 "no" 反而是欺骗性信号，Gmail ML 可识别自动化模式
        #   Importance / X-Priority — 营销/spam 邮件特征，真实商务邮件通常不携带

        # ── 附加纯文本 part（必须在 HTML 之前，RFC 2046 规定后者优先显示） ──
        # 使用 quoted-printable 编码：比 base64 更接近手动发出的商务邮件
        text_part = MIMEText(reply_body, "plain", _UTF8_QP)
        msg.attach(text_part)

        # ── 附加 HTML part（与真实邮件客户端行为一致） ─────────────────────
        html_body = _text_to_html(reply_body, config.SENDER_DISPLAY_NAME)
        html_part = MIMEText(html_body, "html", _UTF8_QP)
        msg.attach(html_part)

        # ── SSL 连接并发送 ─────────────────────────────────────────────────
        logger.info(f"📤 发送回复 → {to_email} | 主题: {reply_subject}")
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


def send_outreach_email(
    *,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
) -> dict:
    """
    发送主动首封开发邮件。

    返回:
      {
        "success": bool,
        "message_id": str,
        "thread_id": str,
        "sent_at": str
      }
    """
    message_id = make_msgid(domain=config.EMAIL_ADDRESS.split("@")[-1])
    sent_at = datetime.now().isoformat()
    sender_domain = config.EMAIL_ADDRESS.split("@")[-1]
    try:
        msg = MIMEMultipart("alternative", boundary=_outlook_boundary())
        msg["From"] = formataddr((config.SENDER_DISPLAY_NAME, config.EMAIL_ADDRESS))
        msg["To"] = formataddr((to_name or to_email, to_email))
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = message_id
        msg["X-Mailer"]   = "Microsoft Outlook 16.0"

        # List-Unsubscribe 是 Gmail 官方指南要求的商业邮件头部，
        # 缺少此头部会显著提升被判为促销/垃圾的概率
        unsubscribe_mailto = f"<mailto:{config.EMAIL_ADDRESS}?subject=unsubscribe>"
        msg["List-Unsubscribe"] = unsubscribe_mailto
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        body_with_footer = _append_unsubscribe_footer(body, config.EMAIL_ADDRESS)
        text_part = MIMEText(body_with_footer, "plain", _UTF8_QP)
        html_part = MIMEText(_text_to_html(body_with_footer, config.SENDER_DISPLAY_NAME), "html", _UTF8_QP)
        msg.attach(text_part)
        msg.attach(html_part)

        logger.info(f"📤 发送开发邮件 → {to_email} | 主题: {subject}")
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=context) as server:
            server.login(config.EMAIL_ADDRESS, config.EMAIL_PASSWORD)
            server.send_message(msg)

        logger.info(f"✅ 开发邮件发送成功 → {to_email}")
        return {
            "success": True,
            "message_id": message_id,
            "thread_id": message_id,
            "sent_at": sent_at,
        }
    except Exception as exc:
        logger.error(f"❌ 开发邮件发送失败: {exc}", exc_info=True)
        return {
            "success": False,
            "message_id": message_id,
            "thread_id": message_id,
            "sent_at": sent_at,
        }


# ─── HTML 生成辅助 ──────────────────────────────────────────────────────────────

import re as _re

def _strip_html_tags(text: str) -> str:
    """移除 LLM 偶尔在纯文本正文里混入的 HTML 标签（<p>, <br>, <a> 等）。"""
    # 先把 <br> / <br/> 转换为换行，再去掉其余所有标签
    text = _re.sub(r'<br\s*/?>', '\n', text, flags=_re.IGNORECASE)
    text = _re.sub(r'<[^>]+>', '', text)
    # 修复 LLM 生成的 </p > 等畸形闭合标签产生的多余空格
    text = _re.sub(r'\s{3,}', '\n\n', text)
    return text.strip()


def _append_unsubscribe_footer(body: str, reply_email: str) -> str:
    """在纯文本正文末尾附加一行退订说明（CAN-SPAM 合规）。"""
    footer = f"\n\n---\nIf you'd prefer not to receive messages like this, reply with 'unsubscribe' and I'll remove you right away."
    return body.rstrip() + footer


def _text_to_html(text: str, sender_name: str) -> str:
    """
    将纯文本正文转换为带基础样式的 HTML。

    生成的 HTML 与 Outlook/Gmail Web 客户端发出的邮件格式高度相似，
    能显著提升 Gmail 对邮件合法性的信任度。

    注意：先对 LLM 输出做 strip_html_tags，避免把模型偶尔生成的
    原始 HTML 标签直接暴露在 HTML part 里（会被 Gmail 视为异常格式）。
    """
    clean_text = _strip_html_tags(text)
    escaped = html_lib.escape(clean_text)
    # 段落之间用空行分隔，每段用 <p> 包裹；行内换行用 <br>
    paragraphs = escaped.split("\n\n")
    body_html = ""
    for para in paragraphs:
        lines = para.strip()
        if lines:
            # 退订说明行用小字灰色样式区分
            if lines.startswith("---"):
                footer_content = lines[3:].strip()
                body_html += (
                    f'<p style="font-size:11px;color:#888888;border-top:1px solid #e5e5e5;'
                    f'padding-top:12px;margin-top:24px;">'
                    f'{footer_content.replace(chr(10), "<br>")}</p>\n'
                )
            else:
                body_html += f"<p>{lines.replace(chr(10), '<br>')}</p>\n"

    return f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 14px;
             color: #222222; line-height: 1.6; margin: 0; padding: 20px;">
  <div style="max-width: 600px;">
    {body_html}
  </div>
</body>
</html>"""
