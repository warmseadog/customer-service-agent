"""
????mailboxes ??processed_messages ??????? thread_id ??????
"""

from __future__ import annotations

import logging
import sqlite3

from app.config import config
from app.thread_scope import SEP, scope_thread_id

logger = logging.getLogger(__name__)


def _ensure_mailboxes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mailboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT 'aliyun',
            email_address TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL DEFAULT '',
            imap_host TEXT NOT NULL DEFAULT '',
            imap_port INTEGER NOT NULL DEFAULT 993,
            smtp_host TEXT NOT NULL DEFAULT '',
            smtp_port INTEGER NOT NULL DEFAULT 465,
            smtp_use_ssl INTEGER NOT NULL DEFAULT 1,
            brand_name TEXT NOT NULL DEFAULT '',
            brand_signature TEXT NOT NULL DEFAULT '',
            sender_display_name TEXT NOT NULL DEFAULT '',
            mail_reply_subject_web_style INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_error TEXT,
            last_checked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _seed_default_mailbox(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM mailboxes ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    now = __import__("datetime").datetime.now().isoformat()
    em = (config.EMAIL_ADDRESS or "").strip()
    if not em:
        logger.warning("??? EMAIL_ADDRESS???????")
        cur = conn.execute(
            """
            INSERT INTO mailboxes (
                label, provider, email_address, password,
                imap_host, imap_port, smtp_host, smtp_port, smtp_use_ssl,
                brand_name, brand_signature, sender_display_name, mail_reply_subject_web_style,
                enabled, created_at, updated_at
            )
            VALUES (?, 'aliyun', 'pending@local', '', 'imap.qiye.aliyun.com', 993,
                    'smtp.qiye.aliyun.com', 465, 1, ?, ?, ?, 1, 0, ?, ?)
            """,
            (
                "???",
                config.BRAND_NAME,
                config.BRAND_SIGNATURE,
                config.SENDER_DISPLAY_NAME,
                now,
                now,
            ),
        )
        return int(cur.lastrowid)
    pw = config.EMAIL_PASSWORD or ""
    br = (config.BRAND_NAME or "").strip() or "Our Brand"
    sig = (config.BRAND_SIGNATURE or "").strip() or "The Partnership Team"
    snd = (config.SENDER_DISPLAY_NAME or "").strip() or "Support Team"
    mrs = 1 if getattr(config, "MAIL_REPLY_SUBJECT_WEB_STYLE", True) else 0
    cur = conn.execute(
        """
        INSERT INTO mailboxes (
            label, provider, email_address, password,
            imap_host, imap_port, smtp_host, smtp_port, smtp_use_ssl,
            brand_name, brand_signature, sender_display_name, mail_reply_subject_web_style,
            enabled, created_at, updated_at
        )
        VALUES (?, 'aliyun', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            br,
            em,
            pw,
            (config.IMAP_HOST or "imap.qiye.aliyun.com").strip(),
            int(config.IMAP_PORT or 993),
            (config.SMTP_HOST or "smtp.qiye.aliyun.com").strip(),
            int(config.SMTP_PORT or 465),
            br,
            sig,
            snd,
            mrs,
            now,
            now,
        ),
    )
    mid = int(cur.lastrowid)
    logger.info("??????? id=%s (%s)", mid, em)
    return mid


def _migrate_processed_messages(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(processed_messages)").fetchall()}
    if "mailbox_id" in cols:
        return
    logger.info("?? processed_messages")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS processed_messages_new (
            mailbox_id INTEGER NOT NULL,
            message_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (mailbox_id, message_id),
            FOREIGN KEY (mailbox_id) REFERENCES mailboxes(id)
        );
        INSERT INTO processed_messages_new (mailbox_id, message_id, thread_id, processed_at)
        SELECT 1, message_id, thread_id, processed_at FROM processed_messages;
        DROP TABLE processed_messages;
        ALTER TABLE processed_messages_new RENAME TO processed_messages;
        """
    )


def _collect_distinct_thread_ids(conn: sqlite3.Connection) -> set[str]:
    s: set[str] = set()
    for q in (
        "SELECT DISTINCT thread_id FROM kol_threads",
        "SELECT DISTINCT thread_id FROM thread_messages",
        "SELECT DISTINCT thread_id FROM intent_results",
        "SELECT DISTINCT thread_id FROM escalation_events",
        "SELECT DISTINCT thread_id FROM outreach_messages WHERE thread_id IS NOT NULL",
        "SELECT DISTINCT thread_id FROM tickets WHERE thread_id IS NOT NULL",
        "SELECT DISTINCT thread_id FROM processed_messages",
    ):
        try:
            for row in conn.execute(q).fetchall():
                tid = row[0]
                if tid:
                    s.add(str(tid))
        except sqlite3.Error:
            continue
    return s


def _migrate_thread_scope(conn: sqlite3.Connection) -> None:
    mids = conn.execute("SELECT id FROM mailboxes ORDER BY id").fetchall()
    if not mids:
        return
    default_mb = int(mids[0]["id"])
    to_map: dict[str, str] = {}
    for tid in _collect_distinct_thread_ids(conn):
        if SEP in tid:
            continue
        to_map[tid] = scope_thread_id(default_mb, tid)
    if not to_map:
        return
    logger.info("????? thread_id %s ?", len(to_map))
    tables = (
        ("kol_threads", "thread_id"),
        ("thread_messages", "thread_id"),
        ("intent_results", "thread_id"),
        ("escalation_events", "thread_id"),
        ("outreach_messages", "thread_id"),
        ("tickets", "thread_id"),
        ("processed_messages", "thread_id"),
    )
    old_pks = list(to_map.keys())
    for tbl, col in tables:
        try:
            for old in old_pks:
                conn.execute(
                    f"UPDATE {tbl} SET {col}=? WHERE {col}=?",
                    (to_map[old], old),
                )
        except sqlite3.Error as e:
            logger.warning("??? %s: %s", tbl, e)

    try:
        rows = conn.execute(
            "SELECT message_id FROM thread_messages WHERE instr(message_id, ?) = 0",
            (SEP,),
        ).fetchall()
        for row in rows:
            oid = row["message_id"]
            nid = scope_thread_id(default_mb, oid)
            conn.execute(
                "UPDATE thread_messages SET message_id=? WHERE message_id=?",
                (nid, oid),
            )
    except sqlite3.Error as e:
        logger.warning("message_id ??: %s", e)


def _ensure_kol_threads_mailbox_id(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kol_threads)").fetchall()}
    if "mailbox_id" not in cols:
        conn.execute("ALTER TABLE kol_threads ADD COLUMN mailbox_id INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        UPDATE kol_threads SET mailbox_id = CAST(substr(thread_id, 1, instr(thread_id, ?) - 1) AS INTEGER)
        WHERE instr(thread_id, ?) > 0
        """,
        (SEP, SEP),
    )
def ensure_mailboxes_schema_and_migrate(conn: sqlite3.Connection) -> None:
    _ensure_mailboxes_table(conn)
    _seed_default_mailbox(conn)
    _migrate_processed_messages(conn)
    _migrate_thread_scope(conn)
    _ensure_kol_threads_mailbox_id(conn)
