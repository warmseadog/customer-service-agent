"""
database.py — SQLite 持久化层

表结构：
  1. 联系人主档（creators）
  2. 产品库（products）含 owner、brand 等字段
  3. 线程状态（kol_threads）
  4. 消息历史（thread_messages）
  5. 意图/情绪识别结果（intent_results）含 cs_sentiment/cs_tone/escalated
  6. 升级事件（escalation_events）
  7. 全局升级收件覆盖（support_escalation_settings，单行）
  8. 内部客服名单（support_staff）：产品负责人下拉选用
  9. 邮箱账户（mailboxes）与多对多关联表 mailbox_products（某邮箱下关键词仅匹配已关联产品）
  10. 外呼历史（campaigns/outreach_messages）— 保留表结构供数据兼容，不再写入新数据
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import config
from app.thread_scope import SEP, scope_thread_id
from app.mailbox_migrate import ensure_mailboxes_schema_and_migrate

logger = logging.getLogger(__name__)


def _get_conn() -> sqlite3.Connection:
    """创建并返回数据库连接，启用 Row 工厂便于字典访问"""
    conn = sqlite3.connect(config.DB_FILE, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA busy_timeout=5000;")
    except sqlite3.Error:
        pass
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
    except sqlite3.Error:
        pass
    return conn


def _now_iso() -> str:
    return datetime.now().isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def _json_loads(value: str | None) -> Any:
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return []


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def _row_to_product(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["keywords"] = _json_loads(data.get("keywords"))
    data["commission_rate"] = float(data.get("commission_rate") or 0)
    data["is_active"] = bool(data.get("is_active", 1))
    return data


def _row_to_creator(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["tags"] = _json_loads(data.get("tags"))
    return data


def _seed_support_staff_if_needed(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT COUNT(1) AS cnt FROM support_staff").fetchone()
    if existing and existing["cnt"]:
        return
    seed_path = Path(config.SUPPORT_STAFF_PATH)
    if not seed_path.exists():
        return
    try:
        items = json.loads(seed_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"⚠️ 内部客服名单种子导入失败: {exc}")
        return
    if not isinstance(items, list) or not items:
        return
    now = _now_iso()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        name = (item.get("display_name") or item.get("name") or "").strip()
        em = (item.get("email") or "").strip().lower()
        if not name or not em:
            continue
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO support_staff (display_name, email, sort_order, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, em, idx, now),
            )
        except Exception:
            pass


def _ensure_mailbox_products_table(conn: sqlite3.Connection) -> None:
    """产品与邮箱账户多对多：仅关联集内产品参与该邮箱的关键词匹配。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mailbox_products (
            mailbox_id  INTEGER NOT NULL,
            product_id  TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (mailbox_id, product_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mailbox_products_product_id
        ON mailbox_products (product_id)
        """
    )


def _seed_products_if_needed(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT COUNT(1) AS cnt FROM products").fetchone()
    if existing and existing["cnt"]:
        return

    seed_path = Path(config.PRODUCTS_PATH)
    if not seed_path.exists():
        return

    try:
        items = json.loads(seed_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"⚠️ 初始产品库导入失败: {exc}")
        return

    now = _now_iso()
    for item in items:
        product_id = item.get("id") or item.get("asin") or f"seed-{abs(hash(item.get('name', '')))}"
        seed_brand = (item.get("brand") or item.get("store_name") or "").strip()
        conn.execute(
            """
            INSERT OR IGNORE INTO products (
                id, name, description, keywords, store_name, asin,
                commission_rate, tagline, scene, intro, is_active,
                owner_name, owner_email, fallback_owner_email,
                brand,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(product_id),
                item.get("name", ""),
                item.get("description") or item.get("intro", ""),
                _json_dumps(item.get("keywords", [])),
                item.get("store_name", config.BRAND_NAME),
                item.get("asin", ""),
                float(item.get("commission_rate") or 0.0),
                item.get("tagline", ""),
                item.get("scene", ""),
                item.get("intro", ""),
                item.get("owner_name", ""),
                item.get("owner_email", ""),
                item.get("fallback_owner_email", ""),
                seed_brand,
                now,
                now,
            ),
        )


def init_db() -> None:
    """初始化数据库，建表并补充轻量迁移。"""
    conn = _get_conn()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id   TEXT PRIMARY KEY,
            thread_id    TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS creators (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            email                TEXT NOT NULL UNIQUE,
            name                 TEXT,
            platform             TEXT,
            profile_url          TEXT,
            country              TEXT,
            language             TEXT,
            tags                 TEXT DEFAULT '[]',
            identity_summary     TEXT,
            notes                TEXT,
            collaboration_status TEXT NOT NULL DEFAULT 'new',
            last_outreach_at     TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            description     TEXT,
            keywords        TEXT NOT NULL DEFAULT '[]',
            store_name      TEXT,
            asin            TEXT,
            commission_rate REAL NOT NULL DEFAULT 0,
            tagline         TEXT,
            scene           TEXT,
            intro           TEXT,
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS campaigns (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            product_id      TEXT,
            commission_rate REAL NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'draft',
            creator_count   INTEGER NOT NULL DEFAULT 0,
            notes           TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS kol_threads (
            thread_id       TEXT PRIMARY KEY,
            kol_email       TEXT NOT NULL,
            kol_name        TEXT,
            creator_id      INTEGER,
            campaign_id     INTEGER,
            product_id      TEXT,
            current_stage   INTEGER NOT NULL DEFAULT 1,
            intent_label    TEXT,
            last_message_id TEXT,
            notes           TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id     TEXT NOT NULL,
            message_id    TEXT NOT NULL UNIQUE,
            role          TEXT NOT NULL CHECK(role IN ('kol', 'our')),
            creator_id    INTEGER,
            campaign_id   INTEGER,
            outreach_id   INTEGER,
            subject       TEXT,
            body          TEXT,
            created_at    TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_thread_messages_thread_id
        ON thread_messages (thread_id, created_at)
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS outreach_messages (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id         INTEGER NOT NULL,
            campaign_id        INTEGER,
            product_id         TEXT,
            thread_id          TEXT NOT NULL,
            message_id         TEXT UNIQUE,
            subject            TEXT NOT NULL,
            body               TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'draft',
            direction          TEXT NOT NULL DEFAULT 'outbound',
            reply_to_message_id TEXT,
            sent_at            TEXT,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS intent_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id       TEXT NOT NULL,
            creator_id      INTEGER,
            campaign_id     INTEGER,
            product_id      TEXT,
            message_id      TEXT,
            intent          TEXT NOT NULL,
            confidence      REAL NOT NULL DEFAULT 0,
            summary         TEXT,
            suggested_reply TEXT,
            raw_json        TEXT,
            cs_sentiment    TEXT,
            cs_tone         TEXT,
            escalated       INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS escalation_events (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id         TEXT NOT NULL,
            creator_id        INTEGER,
            product_id        TEXT,
            reason            TEXT,
            internal_email_to TEXT NOT NULL,
            sent_at           TEXT NOT NULL,
            created_at        TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_escalation_events_thread_id ON escalation_events (thread_id, sent_at)"
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id       INTEGER NOT NULL,
            campaign_id      INTEGER,
            product_id       TEXT,
            thread_id        TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            commission_rate  REAL NOT NULL DEFAULT 0,
            intent           TEXT,
            intent_summary   TEXT,
            latest_message   TEXT,
            notes            TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            UNIQUE(creator_id, campaign_id, product_id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS support_escalation_settings (
            id                   INTEGER PRIMARY KEY CHECK (id = 1),
            backup_owner_email   TEXT,
            backup_owner_name    TEXT,
            default_owner_email  TEXT,
            default_owner_name   TEXT,
            updated_at           TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS support_staff (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name  TEXT NOT NULL,
            email         TEXT NOT NULL UNIQUE,
            sort_order    INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        )
        """
    )
    if not conn.execute(
        "SELECT 1 FROM support_escalation_settings WHERE id = 1"
    ).fetchone():
        conn.execute(
            "INSERT INTO support_escalation_settings (id, updated_at) VALUES (1, ?)",
            (_now_iso(),),
        )

    # migrate old table name if it still exists
    old_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "collaboration_leads" in old_tables and "tickets" not in old_tables:
        conn.execute("ALTER TABLE collaboration_leads RENAME TO tickets")

    _ensure_column(conn, "kol_threads", "creator_id", "INTEGER")
    _ensure_column(conn, "kol_threads", "campaign_id", "INTEGER")
    _ensure_column(conn, "kol_threads", "product_id", "TEXT")
    _ensure_column(conn, "kol_threads", "intent_label", "TEXT")
    _ensure_column(conn, "thread_messages", "creator_id", "INTEGER")
    _ensure_column(conn, "thread_messages", "campaign_id", "INTEGER")
    _ensure_column(conn, "thread_messages", "outreach_id", "INTEGER")
    # products — owner fields
    _ensure_column(conn, "products", "owner_name", "TEXT")
    _ensure_column(conn, "products", "owner_email", "TEXT")
    _ensure_column(conn, "products", "fallback_owner_email", "TEXT")
    _ensure_column(conn, "products", "brand", "TEXT")
    # intent_results — customer service fields
    _ensure_column(conn, "intent_results", "cs_sentiment", "TEXT")
    _ensure_column(conn, "intent_results", "cs_tone", "TEXT")
    _ensure_column(conn, "intent_results", "escalated", "INTEGER NOT NULL DEFAULT 0")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_token ON user_sessions (token)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions (expires_at)"
    )

    ensure_mailboxes_schema_and_migrate(conn)
    _ensure_mailbox_products_table(conn)

    _migrate_users_table_remove_role_check(conn)
    _init_team_rbac_tables(conn)

    _seed_support_staff_if_needed(conn)
    _seed_products_if_needed(conn)
    conn.commit()
    conn.close()
    logger.info("✅ 数据库初始化完成")


# ─── users / sessions（仪表盘 RBAC）──────────────────────────────────────────

_VALID_ROLES = frozenset({"admin", "operator", "viewer", "team_lead", "team_member"})


def _migrate_users_table_remove_role_check(conn: sqlite3.Connection) -> None:
    """SQLite 无法 ALTER CHECK；旧库带 CHECK 时重建 users 以支持 team_lead / team_member。"""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    sql = (row["sql"] or "") if row else ""
    if "CHECK (role IN" not in sql:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS _users_rebuild AS SELECT * FROM users WHERE 0;
        DROP TABLE IF EXISTS _users_rebuild;
        ALTER TABLE user_sessions RENAME TO user_sessions_bak;
        ALTER TABLE users RENAME TO users_old;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO users SELECT * FROM users_old;
        DROP TABLE users_old;
        CREATE TABLE user_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        INSERT INTO user_sessions SELECT * FROM user_sessions_bak;
        DROP TABLE user_sessions_bak;
        CREATE INDEX IF NOT EXISTS idx_user_sessions_token ON user_sessions (token);
        CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions (expires_at);
        """
    )
    conn.execute("PRAGMA foreign_keys=ON")


def _init_team_rbac_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            lead_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_mailboxes (
            team_id INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
            mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
            PRIMARY KEY (team_id, mailbox_id)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_team_mailboxes_mailbox ON team_mailboxes (mailbox_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_mailboxes (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            mailbox_id INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, mailbox_id)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_mailboxes_mailbox ON user_mailboxes (mailbox_id)"
    )


def rbac_mailbox_ids_for_user(user_id: int, role: str) -> frozenset[int] | None:
    """
    None: 不按邮箱过滤（admin / operator / viewer）。
    frozenset: 仅限这些 mailbox_id（team_lead / team_member）；空集表示无权限数据。
    """
    r = (role or "").strip()
    if r in ("admin", "operator", "viewer"):
        return None
    conn = _get_conn()
    try:
        if r == "team_lead":
            rows = conn.execute(
                """
                SELECT tm.mailbox_id FROM team_mailboxes tm
                INNER JOIN teams t ON t.id = tm.team_id AND t.lead_user_id = ?
                """,
                (user_id,),
            ).fetchall()
            return frozenset(int(x["mailbox_id"]) for x in rows)
        if r == "team_member":
            rows = conn.execute(
                "SELECT mailbox_id FROM user_mailboxes WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            return frozenset(int(x["mailbox_id"]) for x in rows)
    finally:
        conn.close()
    return frozenset()


def rbac_lead_can_assign_mailbox(actor_id: int, mailbox_id: int) -> bool:
    """组长仅能指派本组已绑定邮箱给组员。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT 1 FROM team_mailboxes tm
            INNER JOIN teams t ON t.id = tm.team_id AND t.lead_user_id = ?
            WHERE tm.mailbox_id = ?
            LIMIT 1
            """,
            (actor_id, mailbox_id),
        ).fetchone()
        return bool(row)
    finally:
        conn.close()


def rbac_replace_member_mailboxes(user_id: int, mailbox_ids: list[int]) -> None:
    """替换组员邮箱绑定；每个邮箱全局仅能绑定一个 user_mailboxes 行。"""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM user_mailboxes WHERE user_id = ?", (user_id,))
        for mid in sorted(set(mailbox_ids)):
            conn.execute(
                "INSERT OR REPLACE INTO user_mailboxes (user_id, mailbox_id) VALUES (?, ?)",
                (user_id, int(mid)),
            )
        conn.commit()
    finally:
        conn.close()


def rbac_list_teams() -> list[dict]:
    conn = _get_conn()
    try:
        teams = conn.execute("SELECT * FROM teams ORDER BY id").fetchall()
        out = []
        for t in teams:
            d = dict(t)
            mids = conn.execute(
                "SELECT mailbox_id FROM team_mailboxes WHERE team_id = ? ORDER BY mailbox_id",
                (d["id"],),
            ).fetchall()
            d["mailbox_ids"] = [int(r["mailbox_id"]) for r in mids]
            out.append(d)
        return out
    finally:
        conn.close()


def rbac_create_team(*, name: str, lead_user_id: int) -> dict:
    now = _now_iso()
    conn = _get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO teams (name, lead_user_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name.strip(), lead_user_id, now, now),
        )
        tid = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM teams WHERE id = ?", (tid,)).fetchone()
        d = dict(row) if row else {}
        d["mailbox_ids"] = []
        return d
    finally:
        conn.close()


def rbac_update_team(team_id: int, *, name: str | None = None, lead_user_id: int | None = None) -> dict | None:
    conn = _get_conn()
    now = _now_iso()
    try:
        row = conn.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
        if not row:
            return None
        if name is not None:
            conn.execute("UPDATE teams SET name = ?, updated_at = ? WHERE id = ?", (name.strip(), now, team_id))
        if lead_user_id is not None:
            conn.execute(
                "UPDATE teams SET lead_user_id = ?, updated_at = ? WHERE id = ?",
                (lead_user_id, now, team_id),
            )
        conn.commit()
        row2 = conn.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone()
        if not row2:
            return None
        d = dict(row2)
        mids = conn.execute(
            "SELECT mailbox_id FROM team_mailboxes WHERE team_id = ? ORDER BY mailbox_id",
            (team_id,),
        ).fetchall()
        d["mailbox_ids"] = [int(r["mailbox_id"]) for r in mids]
        return d
    finally:
        conn.close()


def rbac_delete_team(team_id: int) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM teams WHERE id = ?", (team_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def rbac_set_team_mailboxes(team_id: int, mailbox_ids: list[int]) -> None:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM team_mailboxes WHERE team_id = ?", (team_id,))
        for mid in sorted(set(int(x) for x in mailbox_ids)):
            conn.execute(
                "INSERT OR IGNORE INTO team_mailboxes (team_id, mailbox_id) VALUES (?, ?)",
                (team_id, mid),
            )
        conn.commit()
    finally:
        conn.close()


def rbac_get_member_mailboxes(user_id: int) -> list[int]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT mailbox_id FROM user_mailboxes WHERE user_id = ? ORDER BY mailbox_id",
            (user_id,),
        ).fetchall()
        return [int(r["mailbox_id"]) for r in rows]
    finally:
        conn.close()


def auth_count_active_admins() -> int:
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT COUNT(1) AS c FROM users
            WHERE role = 'admin' AND is_active = 1
            """,
        ).fetchone()
        return int(row["c"]) if row else 0
    finally:
        conn.close()


def auth_get_user_role(user_id: int) -> str | None:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        return str(row["role"]) if row else None
    finally:
        conn.close()


def auth_count_users() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(1) AS c FROM users").fetchone()
    conn.close()
    return int(row["c"]) if row else 0


def auth_create_user(*, username: str, password_hash: str, role: str) -> dict:
    if role not in _VALID_ROLES:
        raise ValueError("invalid role")
    now = _now_iso()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (username.strip().lower(), password_hash, role, now, now),
        )
        uid = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT id, username, role, is_active, created_at FROM users WHERE id = ?", (uid,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def auth_get_user_by_username(username: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE lower(username) = lower(?)",
        (username.strip(),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def auth_get_user_by_id(user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def auth_list_users() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, username, role, is_active, created_at, updated_at FROM users ORDER BY id"
    ).fetchall()
    conn.close()
    return _dicts(rows)


def auth_update_user(
    user_id: int,
    *,
    password_hash: str | None = None,
    role: str | None = None,
    is_active: int | None = None,
) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return None
    now = _now_iso()
    if password_hash is not None:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now, user_id),
        )
    if role is not None:
        if role not in _VALID_ROLES:
            conn.close()
            raise ValueError("invalid role")
        conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, now, user_id))
    if is_active is not None:
        conn.execute(
            "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if is_active else 0, now, user_id),
        )
    conn.commit()
    row2 = conn.execute(
        "SELECT id, username, role, is_active, created_at, updated_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return dict(row2) if row2 else None


def auth_delete_user(user_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def auth_purge_expired_sessions() -> int:
    conn = _get_conn()
    now = _now_iso()
    cur = conn.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now,))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def auth_create_session(user_id: int, token: str, expires_at: str) -> None:
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO user_sessions (token, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (token, user_id, _now_iso(), expires_at),
    )
    conn.commit()
    conn.close()


def auth_delete_session(token: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def auth_delete_all_sessions_for_user(user_id: int) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def auth_get_session_user(token: str) -> dict | None:
    """有效会话返回用户行 dict（含 password_hash 供校验流程外勿日志）。"""
    conn = _get_conn()
    now = _now_iso()
    row = conn.execute(
        """
        SELECT u.* FROM users u
        JOIN user_sessions s ON s.user_id = u.id
        WHERE s.token = ? AND s.expires_at > ? AND u.is_active = 1
        """,
        (token, now),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ─── processed_messages ───────────────────────────────────────────────────────

def is_message_processed(message_id: str, mailbox_id: int | None = None) -> bool:
    conn = _get_conn()
    if mailbox_id is not None:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE mailbox_id = ? AND message_id = ?",
            (mailbox_id, message_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
    conn.close()
    return row is not None


def mark_message_processed(
    message_id: str, thread_id: str, mailbox_id: int | None = None
) -> None:
    conn = _get_conn()
    if mailbox_id is None:
        row = conn.execute("SELECT id FROM mailboxes ORDER BY id LIMIT 1").fetchone()
        mailbox_id = int(row["id"]) if row else 1
    conn.execute(
        """
        INSERT OR IGNORE INTO processed_messages (mailbox_id, message_id, thread_id, processed_at)
        VALUES (?, ?, ?, ?)
        """,
        (mailbox_id, message_id, thread_id, _now_iso()),
    )
    conn.commit()
    conn.close()


def list_processed_messages(limit: int = 100, mailbox_id: int | None = None) -> list[dict]:
    conn = _get_conn()
    mq = " AND pm.mailbox_id = ? " if mailbox_id is not None else ""
    params: list = []
    if mailbox_id is not None:
        params.append(mailbox_id)
    params.append(limit)
    rows = conn.execute(
        """
        SELECT
            pm.mailbox_id,
            pm.message_id,
            pm.thread_id,
            pm.processed_at,
            kt.kol_email,
            kt.kol_name,
            kt.intent_label,
            IFNULL(mb.label, '') AS mailbox_label,
            IFNULL(mb.email_address, '') AS mailbox_email,
            tm.subject,
            SUBSTR(tm.body, 1, 160) AS body_excerpt
        FROM processed_messages pm
        LEFT JOIN mailboxes mb ON pm.mailbox_id = mb.id
        LEFT JOIN kol_threads kt ON pm.thread_id = kt.thread_id
        LEFT JOIN thread_messages tm
            ON tm.thread_id = pm.thread_id AND tm.message_id = pm.message_id AND tm.role = 'kol'
        WHERE 1=1
        """
        + mq
        + """
        ORDER BY pm.processed_at DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    conn.close()
    return _dicts(rows)


def list_processed_messages_for_mailboxes(limit: int, mailbox_ids: list[int]) -> list[dict]:
    if not mailbox_ids:
        return []
    conn = _get_conn()
    ph = ",".join("?" * len(mailbox_ids))
    mids = tuple(int(x) for x in mailbox_ids)
    params = mids + (limit,)
    rows = conn.execute(
        f"""
        SELECT
            pm.mailbox_id,
            pm.message_id,
            pm.thread_id,
            pm.processed_at,
            kt.kol_email,
            kt.kol_name,
            kt.intent_label,
            IFNULL(mb.label, '') AS mailbox_label,
            IFNULL(mb.email_address, '') AS mailbox_email,
            tm.subject,
            SUBSTR(tm.body, 1, 160) AS body_excerpt
        FROM processed_messages pm
        LEFT JOIN mailboxes mb ON pm.mailbox_id = mb.id
        LEFT JOIN kol_threads kt ON pm.thread_id = kt.thread_id
        LEFT JOIN thread_messages tm
            ON tm.thread_id = pm.thread_id AND tm.message_id = pm.message_id AND tm.role = 'kol'
        WHERE pm.mailbox_id IN ({ph})
        ORDER BY pm.processed_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    conn.close()
    return _dicts(rows)

def list_creators() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM creators ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    conn.close()
    return [_row_to_creator(row) for row in rows if row]


def get_creator(creator_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM creators WHERE id = ?", (creator_id,)).fetchone()
    conn.close()
    return _row_to_creator(row)


def get_creator_by_email(email: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM creators WHERE lower(email) = lower(?)",
        (email.strip(),),
    ).fetchone()
    conn.close()
    return _row_to_creator(row)


def upsert_creator(data: dict) -> dict:
    now = _now_iso()
    email = (data.get("email") or "").strip()
    if not email:
        raise ValueError("达人邮箱不能为空")

    payload = {
        "email": email,
        "name": (data.get("name") or "").strip(),
        "platform": (data.get("platform") or "").strip(),
        "profile_url": (data.get("profile_url") or "").strip(),
        "country": (data.get("country") or "").strip(),
        "language": (data.get("language") or "").strip(),
        "tags": _json_dumps(data.get("tags") or []),
        "identity_summary": (data.get("identity_summary") or "").strip(),
        "notes": (data.get("notes") or "").strip(),
        "collaboration_status": (data.get("collaboration_status") or "new").strip(),
    }

    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO creators (
            email, name, platform, profile_url, country, language, tags,
            identity_summary, notes, collaboration_status, created_at, updated_at
        )
        VALUES (:email, :name, :platform, :profile_url, :country, :language, :tags,
                :identity_summary, :notes, :collaboration_status, :created_at, :updated_at)
        ON CONFLICT(email) DO UPDATE SET
            name = excluded.name,
            platform = excluded.platform,
            profile_url = excluded.profile_url,
            country = excluded.country,
            language = excluded.language,
            tags = excluded.tags,
            identity_summary = excluded.identity_summary,
            notes = excluded.notes,
            collaboration_status = excluded.collaboration_status,
            updated_at = excluded.updated_at
        """,
        payload | {"created_at": now, "updated_at": now},
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM creators WHERE lower(email) = lower(?)",
        (email,),
    ).fetchone()
    conn.close()
    return _row_to_creator(row) or {}


def update_creator(creator_id: int, data: dict) -> dict | None:
    current = get_creator(creator_id)
    if not current:
        return None
    merged = current | data
    merged["tags"] = data.get("tags", current.get("tags", []))
    merged["email"] = (merged.get("email") or current["email"]).strip()
    updated = upsert_creator(merged)
    if data.get("last_outreach_at"):
        conn = _get_conn()
        conn.execute(
            "UPDATE creators SET last_outreach_at = ?, updated_at = ? WHERE id = ?",
            (data["last_outreach_at"], _now_iso(), creator_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM creators WHERE id = ?", (creator_id,)).fetchone()
        conn.close()
        return _row_to_creator(row)
    return updated


def bulk_upsert_creators(items: list[dict]) -> dict:
    created = 0
    updated = 0
    errors: list[dict] = []
    for idx, item in enumerate(items, start=1):
        try:
            before = get_creator_by_email(item.get("email", ""))
            upsert_creator(item)
            if before:
                updated += 1
            else:
                created += 1
        except Exception as exc:
            errors.append({"row": idx, "email": item.get("email", ""), "error": str(exc)})
    return {"created": created, "updated": updated, "errors": errors}


def delete_creator(creator_id: int) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM creators WHERE id = ?", (creator_id,))
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0


def delete_all_creators() -> int:
    conn = _get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM creators")
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes


# ─── products ─────────────────────────────────────────────────────────────────

def list_products(active_only: bool = False, *, with_mailbox_ids: bool = False) -> list[dict]:
    conn = _get_conn()
    sql = "SELECT * FROM products"
    params: tuple[Any, ...] = ()
    if active_only:
        sql += " WHERE is_active = ?"
        params = (1,)
    sql += " ORDER BY updated_at DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    products = [_row_to_product(row) for row in rows if row]
    if with_mailbox_ids and products:
        ids = [str(pr["id"]) for pr in products]
        ph = ",".join("?" * len(ids))
        link_rows = conn.execute(
            f"SELECT product_id, mailbox_id FROM mailbox_products WHERE product_id IN ({ph}) ORDER BY mailbox_id",
            ids,
        ).fetchall()
        by_pid: dict[str, list[int]] = {}
        for lr in link_rows:
            by_pid.setdefault(str(lr["product_id"]), []).append(int(lr["mailbox_id"]))
        for pr in products:
            pr["mailbox_ids"] = by_pid.get(str(pr["id"]), [])
    conn.close()
    return products


def list_products_for_mailbox(mailbox_id: int, *, active_only: bool = True) -> list[dict]:
    conn = _get_conn()
    sql = """
        SELECT p.* FROM products p
        INNER JOIN mailbox_products mp ON mp.product_id = p.id AND mp.mailbox_id = ?
    """
    params: list[Any] = [mailbox_id]
    if active_only:
        sql += " WHERE p.is_active = ?"
        params.append(1)
    sql += " ORDER BY p.updated_at DESC, p.id DESC"
    rows = conn.execute(sql, tuple(params)).fetchall()
    conn.close()
    return [_row_to_product(row) for row in rows if row]


def list_mailbox_ids_for_product(product_id: str) -> list[int]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT mailbox_id FROM mailbox_products WHERE product_id = ? ORDER BY mailbox_id",
        (str(product_id),),
    ).fetchall()
    conn.close()
    return [int(r["mailbox_id"]) for r in rows]


def replace_product_mailboxes(product_id: str, mailbox_ids: list[int]) -> None:
    pid = str(product_id or "").strip()
    if not pid:
        raise ValueError("产品 ID 无效")
    seen: set[int] = set()
    clean: list[int] = []
    for x in mailbox_ids:
        try:
            mid = int(x)
        except (TypeError, ValueError):
            continue
        if mid < 1 or mid in seen:
            continue
        seen.add(mid)
        clean.append(mid)
    conn = _get_conn()
    if clean:
        ph = ",".join("?" * len(clean))
        found = {
            int(r["id"])
            for r in conn.execute(
                f"SELECT id FROM mailboxes WHERE id IN ({ph})", clean
            ).fetchall()
        }
        clean = [m for m in clean if m in found]
    conn.execute("DELETE FROM mailbox_products WHERE product_id = ?", (pid,))
    now = _now_iso()
    for mid in clean:
        conn.execute(
            """
            INSERT INTO mailbox_products (mailbox_id, product_id, created_at)
            VALUES (?, ?, ?)
            """,
            (mid, pid, now),
        )
    conn.commit()
    conn.close()


def get_product(product_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return _row_to_product(row)


def upsert_product(data: dict) -> dict:
    now = _now_iso()
    product_id = str(data.get("id") or "").strip()
    if not product_id:
        raise ValueError("产品 ID 不能为空")
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("产品名称不能为空")

    payload = {
        "id": product_id,
        "name": name,
        "brand": (data.get("brand") or "").strip(),
        "description": (data.get("description") or "").strip(),
        "keywords": _json_dumps(data.get("keywords") or []),
        "store_name": (data.get("store_name") or config.BRAND_NAME).strip(),
        "asin": (data.get("asin") or "").strip(),
        "commission_rate": float(data.get("commission_rate") or 0),
        "tagline": (data.get("tagline") or "").strip(),
        "scene": (data.get("scene") or "").strip(),
        "intro": (data.get("intro") or "").strip(),
        "is_active": 1 if data.get("is_active", True) else 0,
        "owner_name": (data.get("owner_name") or "").strip(),
        "owner_email": (data.get("owner_email") or "").strip(),
        "fallback_owner_email": (data.get("fallback_owner_email") or "").strip(),
        "created_at": now,
        "updated_at": now,
    }

    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO products (
            id, name, brand, description, keywords, store_name, asin,
            commission_rate, tagline, scene, intro, is_active,
            owner_name, owner_email, fallback_owner_email,
            created_at, updated_at
        )
        VALUES (
            :id, :name, :brand, :description, :keywords, :store_name, :asin,
            :commission_rate, :tagline, :scene, :intro, :is_active,
            :owner_name, :owner_email, :fallback_owner_email,
            :created_at, :updated_at
        )
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            brand = excluded.brand,
            description = excluded.description,
            keywords = excluded.keywords,
            store_name = excluded.store_name,
            asin = excluded.asin,
            commission_rate = excluded.commission_rate,
            tagline = excluded.tagline,
            scene = excluded.scene,
            intro = excluded.intro,
            is_active = excluded.is_active,
            owner_name = excluded.owner_name,
            owner_email = excluded.owner_email,
            fallback_owner_email = excluded.fallback_owner_email,
            updated_at = excluded.updated_at
        """,
        payload,
    )
    conn.commit()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return _row_to_product(row) or {}


def delete_product(product_id: str) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM mailbox_products WHERE product_id = ?", (product_id,))
    deleted = conn.execute("DELETE FROM products WHERE id = ?", (product_id,)).rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


def delete_all_products() -> int:
    conn = _get_conn()
    conn.execute("DELETE FROM mailbox_products")
    deleted = conn.execute("DELETE FROM products").rowcount
    conn.commit()
    conn.close()
    return deleted


# ─── support_staff（内部客服，产品负责人下拉）──────────────────────────────────

def list_support_staff() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, display_name, email, sort_order, created_at FROM support_staff ORDER BY sort_order ASC, id ASC"
    ).fetchall()
    conn.close()
    return [
        {
            "id": row["id"],
            "display_name": row["display_name"] or "",
            "email": (row["email"] or "").strip(),
            "sort_order": int(row["sort_order"] or 0),
        }
        for row in rows
    ]


def insert_support_staff(*, display_name: str, email: str) -> dict:
    name = (display_name or "").strip()
    em = (email or "").strip().lower()
    if not name:
        raise ValueError("姓名为空")
    if not em or "@" not in em:
        raise ValueError("邮箱无效")
    now = _now_iso()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO support_staff (display_name, email, sort_order, created_at)
            VALUES (?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM support_staff), ?)
            """,
            (name, em, now),
        )
        sid = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.close()
        raise ValueError("该邮箱已存在") from e
    except Exception:
        conn.close()
        raise
    row = conn.execute("SELECT * FROM support_staff WHERE id = ?", (sid,)).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("插入失败")
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "email": row["email"],
        "sort_order": row["sort_order"],
    }


def delete_support_staff(staff_id: int) -> bool:
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM support_staff WHERE id = ?", (staff_id,)).rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


# ─── campaigns / outreach ─────────────────────────────────────────────────────

def create_campaign(name: str, product_id: str, commission_rate: float, notes: str = "") -> dict:
    now = _now_iso()
    conn = _get_conn()
    cursor = conn.execute(
        """
        INSERT INTO campaigns (name, product_id, commission_rate, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name.strip(), product_id, float(commission_rate or 0), notes.strip(), now, now),
    )
    campaign_id = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def list_campaigns() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM campaigns ORDER BY updated_at DESC, id DESC").fetchall()
    conn.close()
    return _dicts(rows)


def get_campaign(campaign_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_campaign(campaign_id: int) -> bool:
    conn = _get_conn()
    cursor = conn.cursor()
    # Delete associated outreach messages (drafts) first
    cursor.execute("DELETE FROM outreach_messages WHERE campaign_id = ?", (campaign_id,))
    # Delete the campaign itself
    cursor.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    changes = cursor.rowcount
    conn.commit()
    conn.close()
    return changes > 0


def update_campaign(campaign_id: int, **fields: Any) -> dict | None:
    current = get_campaign(campaign_id)
    if not current:
        return None
    allowed = {"name", "product_id", "commission_rate", "status", "creator_count", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return current
    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    conn = _get_conn()
    conn.execute(
        f"UPDATE campaigns SET {set_clause} WHERE id = :campaign_id",
        updates | {"campaign_id": campaign_id},
    )
    conn.commit()
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_outreach_message(data: dict) -> dict:
    now = _now_iso()
    conn = _get_conn()
    cursor = conn.execute(
        """
        INSERT INTO outreach_messages (
            creator_id, campaign_id, product_id, thread_id, message_id, subject, body,
            status, direction, reply_to_message_id, sent_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["creator_id"],
            data.get("campaign_id"),
            data.get("product_id"),
            data["thread_id"],
            data.get("message_id"),
            data.get("subject", ""),
            data.get("body", ""),
            data.get("status", "draft"),
            data.get("direction", "outbound"),
            data.get("reply_to_message_id"),
            data.get("sent_at"),
            now,
            now,
        ),
    )
    msg_id = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM outreach_messages WHERE id = ?", (msg_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def update_outreach_message(outreach_id: int, **fields: Any) -> dict | None:
    allowed = {
        "subject",
        "body",
        "status",
        "message_id",
        "sent_at",
        "reply_to_message_id",
        "thread_id",
        "campaign_id",
        "product_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_outreach_message(outreach_id)
    updates["updated_at"] = _now_iso()
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    conn = _get_conn()
    conn.execute(
        f"UPDATE outreach_messages SET {set_clause} WHERE id = :mid",
        updates | {"mid": outreach_id},
    )
    conn.commit()
    row = conn.execute("SELECT * FROM outreach_messages WHERE id = ?", (outreach_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_outreach_message(message_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM outreach_messages WHERE id = ?", (message_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_outreach_message_by_rfc_message_id(rfc_message_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM outreach_messages WHERE message_id = ?",
        (rfc_message_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_outreach_message_by_thread_id(thread_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT * FROM outreach_messages
        WHERE thread_id = ?
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (thread_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_outreach_message(outreach_id: int) -> bool:
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM outreach_messages WHERE id = ?", (outreach_id,)).rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


def list_outreach_messages(campaign_id: int | None = None) -> list[dict]:
    conn = _get_conn()
    if campaign_id is None:
        rows = conn.execute(
            "SELECT * FROM outreach_messages ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM outreach_messages
            WHERE campaign_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (campaign_id,),
        ).fetchall()
    conn.close()
    return _dicts(rows)


# ─── intents / leads ──────────────────────────────────────────────────────────

def create_intent_result(data: dict) -> dict:
    now = _now_iso()
    conn = _get_conn()
    cursor = conn.execute(
        """
        INSERT INTO intent_results (
            thread_id, creator_id, campaign_id, product_id, message_id,
            intent, confidence, summary, suggested_reply, raw_json,
            cs_sentiment, cs_tone, escalated, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("thread_id", ""),
            data.get("creator_id"),
            data.get("campaign_id"),
            data.get("product_id"),
            data.get("message_id"),
            data.get("intent", "cs_reply"),
            float(data.get("confidence") or 0),
            data.get("summary", ""),
            data.get("suggested_reply", ""),
            data.get("raw_json", ""),
            data.get("cs_sentiment", ""),
            data.get("cs_tone", ""),
            1 if data.get("escalated") else 0,
            now,
        ),
    )
    rid = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM intent_results WHERE id = ?", (rid,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def delete_intent_result(intent_id: int) -> bool:
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM intent_results WHERE id = ?", (intent_id,)).rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


def delete_all_intent_results() -> int:
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM intent_results").rowcount
    conn.commit()
    conn.close()
    return deleted


def check_repeat_dissatisfaction(thread_id: str, hours: int) -> bool:
    """判断该线程在指定小时内是否曾出现过 dissatisfied 情绪（排除最新一条）。"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT COUNT(1) AS cnt FROM intent_results
        WHERE thread_id = ? AND cs_sentiment = 'dissatisfied' AND created_at >= ?
        """,
        (thread_id, cutoff),
    ).fetchone()
    conn.close()
    return (row["cnt"] if row else 0) > 0


def create_escalation_event(data: dict) -> dict:
    now = _now_iso()
    conn = _get_conn()
    cursor = conn.execute(
        """
        INSERT INTO escalation_events (
            thread_id, creator_id, product_id, reason, internal_email_to, sent_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("thread_id", ""),
            data.get("creator_id"),
            data.get("product_id"),
            data.get("reason", ""),
            data.get("internal_email_to", ""),
            data.get("sent_at", now),
            now,
        ),
    )
    eid = cursor.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM escalation_events WHERE id = ?", (eid,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def list_escalation_events(limit: int = 100) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT ee.*,
               c.email AS creator_email, c.name AS creator_name,
               p.name AS product_name
        FROM escalation_events ee
        LEFT JOIN creators c ON ee.creator_id = c.id
        LEFT JOIN products p ON ee.product_id = p.id
        ORDER BY ee.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return _dicts(rows)


def list_escalation_events_for_mailboxes(limit: int, mailbox_ids: list[int]) -> list[dict]:
    if not mailbox_ids:
        return []
    conn = _get_conn()
    ph = ",".join("?" * len(mailbox_ids))
    mids = tuple(int(x) for x in mailbox_ids)
    rows = conn.execute(
        f"""
        SELECT ee.*,
               c.email AS creator_email, c.name AS creator_name,
               p.name AS product_name
        FROM escalation_events ee
        LEFT JOIN creators c ON ee.creator_id = c.id
        LEFT JOIN products p ON ee.product_id = p.id
        INNER JOIN kol_threads kt ON kt.thread_id = ee.thread_id AND kt.mailbox_id IN ({ph})
        ORDER BY ee.created_at DESC
        LIMIT ?
        """,
        mids + (limit,),
    ).fetchall()
    conn.close()
    return _dicts(rows)


def get_escalation_event(escalation_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM escalation_events WHERE id = ?", (escalation_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_last_escalation_time(thread_id: str) -> str | None:
    """返回该线程最近一次升级的 sent_at ISO 字符串，若无则返回 None。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT sent_at FROM escalation_events WHERE thread_id = ? ORDER BY sent_at DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    conn.close()
    return row["sent_at"] if row else None


def count_escalation_events_for_thread(thread_id: str) -> int:
    """该线程已成功记录的升级事件条数（发内部邮件前统计，用于标注第几次推送）。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(1) AS cnt FROM escalation_events WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    return int(row["cnt"]) if row and row["cnt"] is not None else 0


def count_escalation_events() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(1) AS cnt FROM escalation_events").fetchone()
    conn.close()
    return row["cnt"] if row else 0


def delete_escalation_event(escalation_id: int) -> bool:
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM escalation_events WHERE id = ?", (escalation_id,)).rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


def delete_all_escalation_events() -> int:
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM escalation_events").rowcount
    conn.commit()
    conn.close()
    return deleted


def list_intent_results(limit: int = 100) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT ir.*, c.email AS creator_email, c.name AS creator_name
        FROM intent_results ir
        LEFT JOIN creators c ON ir.creator_id = c.id
        ORDER BY ir.created_at DESC, ir.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return _dicts(rows)


def list_intent_results_for_mailboxes(limit: int, mailbox_ids: list[int]) -> list[dict]:
    if not mailbox_ids:
        return []
    conn = _get_conn()
    ph = ",".join("?" * len(mailbox_ids))
    mids = tuple(int(x) for x in mailbox_ids)
    rows = conn.execute(
        f"""
        SELECT ir.*, c.email AS creator_email, c.name AS creator_name
        FROM intent_results ir
        LEFT JOIN creators c ON ir.creator_id = c.id
        INNER JOIN kol_threads kt ON kt.thread_id = ir.thread_id AND kt.mailbox_id IN ({ph})
        ORDER BY ir.created_at DESC, ir.id DESC
        LIMIT ?
        """,
        mids + (limit,),
    ).fetchall()
    conn.close()
    return _dicts(rows)


def get_intent_result(intent_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM intent_results WHERE id = ?", (intent_id,)).fetchone()
    conn.close()
    return dict(row) if row else None



def update_ticket_status(ticket_id: int, status: str) -> dict | None:
    allowed = {"new", "in_progress", "done"}
    if status not in allowed:
        return None
    now = _now_iso()
    conn = _get_conn()
    conn.execute(
        "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, ticket_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_ticket(ticket_id: int) -> bool:
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,)).rowcount
    conn.commit()
    conn.close()
    return bool(deleted)


def delete_all_tickets() -> int:
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM tickets").rowcount
    conn.commit()
    conn.close()
    return deleted


def list_collaboration_leads() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT
            cl.*,
            c.email AS creator_email,
            c.name AS creator_name,
            c.platform,
            p.name AS product_name,
            p.asin,
            cam.name AS campaign_name
        FROM tickets cl
        LEFT JOIN creators c ON cl.creator_id = c.id
        LEFT JOIN products p ON cl.product_id = p.id
        LEFT JOIN campaigns cam ON cl.campaign_id = cam.id
        ORDER BY cl.updated_at DESC, cl.id DESC
        """
        ).fetchall()
    conn.close()
    return _dicts(rows)


def count_pending_tickets() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(1) AS cnt FROM tickets WHERE status = 'new'").fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ─── thread / message history ─────────────────────────────────────────────────

def get_thread_state(thread_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM kol_threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_thread_state(
    thread_id: str,
    kol_email: str,
    kol_name: str,
    stage: int,
    last_message_id: str,
    notes: str = "",
    creator_id: int | None = None,
    campaign_id: int | None = None,
    product_id: str | None = None,
    intent_label: str | None = None,
) -> None:
    conn = _get_conn()
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO kol_threads (
            thread_id, kol_email, kol_name, creator_id, campaign_id, product_id,
            current_stage, intent_label, last_message_id, notes, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            kol_email = excluded.kol_email,
            kol_name = excluded.kol_name,
            creator_id = COALESCE(excluded.creator_id, kol_threads.creator_id),
            campaign_id = COALESCE(excluded.campaign_id, kol_threads.campaign_id),
            product_id = COALESCE(excluded.product_id, kol_threads.product_id),
            current_stage = excluded.current_stage,
            intent_label = COALESCE(excluded.intent_label, kol_threads.intent_label),
            last_message_id = excluded.last_message_id,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (
            thread_id,
            kol_email,
            kol_name,
            creator_id,
            campaign_id,
            product_id,
            stage,
            intent_label,
            last_message_id,
            notes,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()


def list_all_threads(mailbox_id: int | None = None) -> list[dict]:
    conn = _get_conn()
    if mailbox_id is not None:
        rows = conn.execute(
            """
            SELECT kt.*, mb.label AS mailbox_label, mb.email_address AS mailbox_email
            FROM kol_threads kt
            LEFT JOIN mailboxes mb ON kt.mailbox_id = mb.id
            WHERE kt.mailbox_id = ?
            ORDER BY kt.updated_at DESC
            """
            ,
            (mailbox_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT kt.*, mb.label AS mailbox_label, mb.email_address AS mailbox_email
            FROM kol_threads kt
            LEFT JOIN mailboxes mb ON kt.mailbox_id = mb.id
            ORDER BY kt.updated_at DESC
            """
        ).fetchall()
    conn.close()
    return _dicts(rows)


def list_all_threads_mailboxes(mailbox_ids: list[int]) -> list[dict]:
    if not mailbox_ids:
        return []
    conn = _get_conn()
    ph = ",".join("?" * len(mailbox_ids))
    rows = conn.execute(
        f"""
        SELECT kt.*, mb.label AS mailbox_label, mb.email_address AS mailbox_email
        FROM kol_threads kt
        LEFT JOIN mailboxes mb ON kt.mailbox_id = mb.id
        WHERE kt.mailbox_id IN ({ph})
        ORDER BY kt.updated_at DESC
        """,
        tuple(int(x) for x in mailbox_ids),
    ).fetchall()
    conn.close()
    return _dicts(rows)


def save_thread_message(
    thread_id: str,
    message_id: str,
    role: str,
    subject: str,
    body: str,
    created_at: str | None = None,
    creator_id: int | None = None,
    campaign_id: int | None = None,
    outreach_id: int | None = None,
) -> None:
    if role not in ("kol", "our"):
        raise ValueError(f"role 必须为 'kol' 或 'our'，实际为: {role!r}")

    ts = created_at or _now_iso()
    conn = _get_conn()
    conn.execute(
        """
        INSERT OR IGNORE INTO thread_messages
            (thread_id, message_id, role, creator_id, campaign_id, outreach_id, subject, body, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            message_id,
            role,
            creator_id,
            campaign_id,
            outreach_id,
            subject or "",
            body or "",
            ts,
        ),
    )
    conn.commit()
    conn.close()


def get_thread_messages(thread_id: str, limit: int | None = None) -> list[dict]:
    effective_limit = limit or config.MAX_THREAD_MESSAGES
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT thread_id, message_id, role, creator_id, campaign_id, outreach_id, subject, body, created_at
            FROM thread_messages
            WHERE thread_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        )
        ORDER BY created_at ASC
        """,
        (thread_id, effective_limit),
    ).fetchall()
    conn.close()
    return _dicts(rows)


def delete_thread(thread_id: str) -> int:
    conn = _get_conn()
    deleted = 0
    deleted += conn.execute("DELETE FROM thread_messages WHERE thread_id = ?", (thread_id,)).rowcount
    deleted += conn.execute("DELETE FROM processed_messages WHERE thread_id = ?", (thread_id,)).rowcount
    deleted += conn.execute("DELETE FROM kol_threads WHERE thread_id = ?", (thread_id,)).rowcount
    deleted += conn.execute("DELETE FROM outreach_messages WHERE thread_id = ?", (thread_id,)).rowcount
    deleted += conn.execute("DELETE FROM intent_results WHERE thread_id = ?", (thread_id,)).rowcount
    deleted += conn.execute("DELETE FROM tickets WHERE thread_id = ?", (thread_id,)).rowcount
    conn.commit()
    conn.close()
    return deleted


def clear_all_thread_data() -> dict:
    """
    清空全部线程相关数据：对话历史、线程状态、已处理邮件去重。
    不删除：外呼草稿/已发记录、意图识别流水、工单、达人、产品等。
    """
    conn = _get_conn()
    result = {
        "thread_messages": conn.execute("DELETE FROM thread_messages").rowcount,
        "processed_messages": conn.execute("DELETE FROM processed_messages").rowcount,
        "kol_threads": conn.execute("DELETE FROM kol_threads").rowcount,
    }
    conn.commit()
    conn.close()
    return result


def delete_all_data() -> dict:
    conn = _get_conn()
    result = {
        "thread_messages": conn.execute("DELETE FROM thread_messages").rowcount,
        "processed_messages": conn.execute("DELETE FROM processed_messages").rowcount,
        "kol_threads": conn.execute("DELETE FROM kol_threads").rowcount,
        "outreach_messages": conn.execute("DELETE FROM outreach_messages").rowcount,
        "intent_results": conn.execute("DELETE FROM intent_results").rowcount,
        "escalation_events": conn.execute("DELETE FROM escalation_events").rowcount,
        "tickets": conn.execute("DELETE FROM tickets").rowcount,
        "campaigns": conn.execute("DELETE FROM campaigns").rowcount,
        "creators": conn.execute("DELETE FROM creators").rowcount,
        "mailbox_products": conn.execute("DELETE FROM mailbox_products").rowcount,
        "products": conn.execute("DELETE FROM products").rowcount,
    }
    conn.commit()
    _seed_products_if_needed(conn)
    conn.commit()
    conn.close()
    return result


# ─── support_escalation_settings（全局升级收件，仪表盘可覆盖 .env）────────────────

def get_support_escalation_settings() -> dict:
    """单行配置；字段可为 None 表示未在数据库中覆盖，将回退 .env。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM support_escalation_settings WHERE id = 1"
    ).fetchone()
    conn.close()
    if not row:
        return {
            "default_owner_email": None,
            "default_owner_name": None,
            "updated_at": None,
        }
    return dict(row)


def upsert_support_escalation_settings(
    *,
    default_owner_email: str | None,
    default_owner_name: str | None,
) -> dict:
    def _norm(s: str | None) -> str | None:
        if s is None:
            return None
        t = str(s).strip()
        return t if t else None

    payload = {
        "default_owner_email": _norm(default_owner_email),
        "default_owner_name": _norm(default_owner_name),
        "updated_at": _now_iso(),
    }
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO support_escalation_settings (
            id, default_owner_email, default_owner_name, updated_at
        )
        VALUES (1, :default_owner_email, :default_owner_name, :updated_at)
        ON CONFLICT(id) DO UPDATE SET
            default_owner_email = excluded.default_owner_email,
            default_owner_name = excluded.default_owner_name,
            updated_at = excluded.updated_at
        """,
        payload,
    )
    conn.commit()
    conn.close()
    return get_support_escalation_settings()


# --- mailboxes ---------------------------------------------------------------

def _row_mailbox(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row) if not isinstance(row, dict) else dict(row)
    pw = d.get("password") or ""
    d["has_password"] = bool(str(pw).strip())
    d.pop("password", None)
    d["mail_reply_subject_web_style"] = bool(d.get("mail_reply_subject_web_style", 1))
    d["enabled"] = bool(d.get("enabled", 1))
    d["smtp_use_ssl"] = bool(d.get("smtp_use_ssl", 1))
    return d


def get_mailbox_raw(mailbox_id: int) -> dict | None:
    """含 password，仅供发信拉信用。"""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM mailboxes WHERE id = ?", (mailbox_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_mailboxes() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM mailboxes ORDER BY id ASC").fetchall()
    conn.close()
    return [_row_mailbox(r) for r in rows]


def list_enabled_mailboxes() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM mailboxes WHERE enabled = 1 ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]  # noqa: SIM115


def insert_mailbox(payload: dict) -> dict:
    now = _now_iso()
    conn = _get_conn()
    email = (payload.get("email_address") or "").strip()
    if not email or "@" not in email:
        conn.close()
        raise ValueError("email_address required")
    pw = (payload.get("password") or "").strip()
    cur = conn.execute(
        """
        INSERT INTO mailboxes (
            label, provider, email_address, password,
            imap_host, imap_port, smtp_host, smtp_port, smtp_use_ssl,
            brand_name, brand_signature, sender_display_name, mail_reply_subject_web_style,
            enabled, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (payload.get("label") or "").strip() or email,
            (payload.get("provider") or "aliyun").strip(),
            email,
            pw,
            (payload.get("imap_host") or "").strip(),
            int(payload.get("imap_port") or 993),
            (payload.get("smtp_host") or "").strip(),
            int(payload.get("smtp_port") or 465),
            1 if payload.get("smtp_use_ssl", True) else 0,
            (payload.get("brand_name") or "").strip(),
            (payload.get("brand_signature") or "").strip(),
            (payload.get("sender_display_name") or "").strip(),
            1 if payload.get("mail_reply_subject_web_style", True) else 0,
            1 if payload.get("enabled", True) else 0,
            now,
            now,
        ),
    )
    mid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    row = get_mailbox_raw(mid)
    return _row_mailbox(row)


def update_mailbox(mailbox_id: int, payload: dict) -> dict | None:
    cur = get_mailbox_raw(mailbox_id)
    if not cur:
        return None
    now = _now_iso()
    sets = []
    vals: list = []
    mapping = {
        "label": "label",
        "provider": "provider",
        "email_address": "email_address",
        "imap_host": "imap_host",
        "imap_port": "imap_port",
        "smtp_host": "smtp_host",
        "smtp_port": "smtp_port",
        "brand_name": "brand_name",
        "brand_signature": "brand_signature",
        "sender_display_name": "sender_display_name",
    }
    if "smtp_use_ssl" in payload:
        sets.append("smtp_use_ssl = ?")
        vals.append(1 if payload.get("smtp_use_ssl") else 0)
    if "enabled" in payload:
        sets.append("enabled = ?")
        vals.append(1 if payload.get("enabled") else 0)
    if "mail_reply_subject_web_style" in payload:
        sets.append("mail_reply_subject_web_style = ?")
        vals.append(1 if payload.get("mail_reply_subject_web_style") else 0)
    if "password" in payload:
        pv = payload.get("password")
        if isinstance(pv, str) and pv.strip():
            sets.append("password = ?")
            vals.append(pv.strip())
    for py, db in mapping.items():
        if py not in payload:
            continue
        sets.append(f"{db} = ?")
        if py in ("imap_port", "smtp_port"):
            vals.append(int(payload.get(py) or 0))
        else:
            vals.append(str(payload.get(py) or "").strip())

    sets.append("updated_at = ?")
    vals.append(now)
    vals.append(mailbox_id)
    if len(sets) <= 1:
        row = get_mailbox_raw(mailbox_id)
        return _row_mailbox(row) if row else None
    conn = _get_conn()
    sql = "UPDATE mailboxes SET " + ", ".join(sets) + " WHERE id = ?"
    conn.execute(sql, vals)
    conn.commit()
    conn.close()
    row = get_mailbox_raw(mailbox_id)
    return _row_mailbox(row) if row else None


def delete_mailbox(mailbox_id: int) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM mailbox_products WHERE mailbox_id = ?", (mailbox_id,))
    n = conn.execute("DELETE FROM mailboxes WHERE id = ?", (mailbox_id,)).rowcount
    conn.commit()
    conn.close()
    return n > 0


def update_mailbox_check_status(
    mailbox_id: int, *, last_error: str | None = None, last_checked_at: str | None = None
) -> None:
    conn = _get_conn()
    if last_checked_at:
        conn.execute(
            """
            UPDATE mailboxes SET last_checked_at = ?, last_error = ?, updated_at = ? WHERE id = ?
            """
            ,
            (
                last_checked_at,
                last_error,
                last_checked_at,
                mailbox_id,
            ),
        )
    elif last_error is not None:
        conn.execute(
            """
            UPDATE mailboxes SET last_error = ?, updated_at = ? WHERE id = ?
            """
            ,
            (last_error, _now_iso(), mailbox_id),
        )
    conn.commit()
    conn.close()
