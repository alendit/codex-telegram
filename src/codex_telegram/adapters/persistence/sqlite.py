"""SQLite-backed persistence adapters."""

from __future__ import annotations

import aiosqlite
from datetime import UTC, datetime
import json
from pathlib import Path
import secrets

from codex_telegram.application.models import (
    CallbackToken,
    FinalMessageState,
    PendingReplyTarget,
    ProgressMessageState,
    StatusCardState,
)
from codex_telegram.domain import (
    AttachmentJob,
    BridgeControlJob,
    BridgeThread,
    ConversationAnchor,
    DirectoryEntry,
    LogicalThread,
    PendingApproval,
    PendingUserInput,
    Project,
    SessionOverrides,
    ThreadMessage,
    UserInputOption,
    UserInputQuestion,
    WebhookSubscription,
)

THREAD_MESSAGE_LIMIT = 200


def utcnow() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(UTC).isoformat()


class SQLiteStateRepository:
    """Durable core state for chats, threads, overrides, and approvals."""

    def __init__(self, path: Path, default_backend_id: str = "primary") -> None:
        self._path = path
        self._default_backend_id = default_backend_id

    async def initialize(self) -> None:
        """Create the SQLite schema if missing."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.executescript("""
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS chats (
                    chat_key TEXT PRIMARY KEY,
                    active_thread_id TEXT,
                    focused_bridge_id TEXT,
                    previous_thread_id TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_anchors (
                    anchor_id TEXT PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    codex_backend_id TEXT NOT NULL DEFAULT 'primary',
                    codex_thread_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    alias TEXT,
                    project_id TEXT,
                    latest_bridge_id TEXT,
                    broken_reason TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_key, codex_backend_id, codex_thread_id)
                );

                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    anchor_id TEXT,
                    title TEXT NOT NULL,
                    codex_thread_id TEXT,
                    codex_backend_id TEXT NOT NULL DEFAULT 'primary',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    awaiting_reply INTEGER NOT NULL DEFAULT 0,
                    interrupted_notice INTEGER NOT NULL DEFAULT 0,
                    pending_turn_id TEXT,
                    expires_at TEXT,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS bridge_threads (
                    bridge_id TEXT PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    anchor_id TEXT,
                    title TEXT NOT NULL,
                    codex_thread_id TEXT,
                    codex_backend_id TEXT NOT NULL DEFAULT 'primary',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    awaiting_reply INTEGER NOT NULL DEFAULT 0,
                    interrupted_notice INTEGER NOT NULL DEFAULT 0,
                    pending_turn_id TEXT,
                    expires_at TEXT,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS overrides (
                    thread_id TEXT PRIMARY KEY,
                    profile TEXT,
                    model TEXT,
                    effort TEXT,
                    summary TEXT,
                    cwd TEXT,
                    fast_mode INTEGER,
                    fast_mode_is_set INTEGER NOT NULL DEFAULT 0,
                    verbosity TEXT,
                    command_verbosity TEXT,
                    followup_mode TEXT,
                    collaboration_mode TEXT
                );

                CREATE TABLE IF NOT EXISTS pending_requests (
                    request_id INTEGER PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    logical_thread_id TEXT NOT NULL,
                    codex_thread_id TEXT NOT NULL,
                    codex_backend_id TEXT NOT NULL DEFAULT 'primary',
                    turn_id TEXT,
                    method TEXT NOT NULL,
                    command_text TEXT,
                    reason TEXT,
                    approval_message TEXT,
                    raw_params TEXT NOT NULL,
                    session_scope INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_user_inputs (
                    request_id INTEGER PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    logical_thread_id TEXT NOT NULL,
                    codex_thread_id TEXT NOT NULL,
                    codex_backend_id TEXT NOT NULL DEFAULT 'primary',
                    turn_id TEXT,
                    method TEXT NOT NULL,
                    questions_json TEXT NOT NULL,
                    selected_answers_json TEXT NOT NULL,
                    awaiting_free_text_question_id TEXT,
                    raw_params TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS thread_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    turn_id TEXT
                );

                CREATE TABLE IF NOT EXISTS thread_delivery_watermarks (
                    chat_key TEXT NOT NULL,
                    anchor_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    last_message_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_key, anchor_id, thread_id)
                );

                CREATE TABLE IF NOT EXISTS thread_directories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    selected_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attachment_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_key TEXT NOT NULL,
                    logical_thread_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    caption TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bridge_control_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_key TEXT NOT NULL,
                    logical_thread_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    connection_id TEXT NOT NULL,
                    root_path TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(connection_id, root_path)
                );

                CREATE TABLE IF NOT EXISTS thread_projects (
                    thread_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_overrides (
                    project_id TEXT PRIMARY KEY,
                    model TEXT,
                    effort TEXT,
                    fast_mode INTEGER,
                    fast_mode_is_set INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS webhook_subscriptions (
                    webhook_id TEXT PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    thread_id TEXT,
                    anchor_id TEXT,
                    name TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    trigger_count INTEGER NOT NULL DEFAULT 0,
                    last_triggered_at TEXT
                );

                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    webhook_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(webhook_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_chat
                    ON webhook_subscriptions(chat_key, enabled);

                CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_thread
                    ON webhook_subscriptions(thread_id, enabled);

                CREATE INDEX IF NOT EXISTS idx_bridge_threads_chat
                    ON bridge_threads(chat_key, updated_at);

                CREATE INDEX IF NOT EXISTS idx_conversation_anchors_chat
                    ON conversation_anchors(chat_key, updated_at);

                CREATE TABLE IF NOT EXISTS callback_tokens (
                    token TEXT PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    topic_id INTEGER,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_callback_tokens_expires
                    ON callback_tokens(expires_at);
                """)
            await self._migrate_legacy_workspace_tables(db)
            await _ensure_thread_delivery_watermarks_thread_scoped(db)
            await _ensure_column(
                db,
                table_name="chats",
                column_name="focused_bridge_id",
                definition="TEXT",
            )
            for column_name, definition in (
                ("codex_backend_id", "TEXT NOT NULL DEFAULT 'primary'"),
                ("anchor_id", "TEXT"),
                ("expires_at", "TEXT"),
                ("closed_at", "TEXT"),
            ):
                await _ensure_column(
                    db,
                    table_name="threads",
                    column_name=column_name,
                    definition=definition,
                )
            await db.execute(
                "UPDATE threads SET codex_backend_id = ? WHERE codex_backend_id = 'primary'",
                (self._default_backend_id,),
            )
            for column_name, definition in (
                ("thread_id", "TEXT"),
                ("anchor_id", "TEXT"),
            ):
                await _ensure_column(
                    db,
                    table_name="webhook_subscriptions",
                    column_name=column_name,
                    definition=definition,
                )
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_anchor
                    ON webhook_subscriptions(anchor_id, enabled)
            """)
            await self._migrate_legacy_threads_to_anchors_and_bridges(db)
            for column_name, definition in (
                ("codex_backend_id", "TEXT NOT NULL DEFAULT 'primary'"),
                ("approval_message", "TEXT"),
            ):
                await _ensure_column(
                    db,
                    table_name="pending_requests",
                    column_name=column_name,
                    definition=definition,
                )
            await db.execute(
                """
                UPDATE pending_requests
                   SET codex_backend_id = ?
                 WHERE codex_backend_id = 'primary'
                """,
                (self._default_backend_id,),
            )
            for column_name, definition in (
                ("codex_backend_id", "TEXT NOT NULL DEFAULT 'primary'"),
            ):
                await _ensure_column(
                    db,
                    table_name="pending_user_inputs",
                    column_name=column_name,
                    definition=definition,
                )
            await db.execute(
                """
                UPDATE pending_user_inputs
                   SET codex_backend_id = ?
                 WHERE codex_backend_id = 'primary'
                """,
                (self._default_backend_id,),
            )
            for column_name, definition in (
                ("fast_mode", "INTEGER"),
                ("fast_mode_is_set", "INTEGER NOT NULL DEFAULT 0"),
            ):
                await _ensure_column(
                    db,
                    table_name="project_overrides",
                    column_name=column_name,
                    definition=definition,
                )
            for column_name, definition in (
                ("fast_mode", "INTEGER"),
                ("fast_mode_is_set", "INTEGER NOT NULL DEFAULT 0"),
                ("verbosity", "TEXT"),
                ("command_verbosity", "TEXT"),
                ("followup_mode", "TEXT"),
                ("collaboration_mode", "TEXT"),
            ):
                await _ensure_column(
                    db,
                    table_name="overrides",
                    column_name=column_name,
                    definition=definition,
                )
            await _ensure_overrides_fast_mode_nullable(db)
            await db.execute("""
                UPDATE overrides
                   SET fast_mode_is_set = 1
                 WHERE fast_mode IS NOT NULL
                   AND fast_mode_is_set = 0
                """)
            await db.commit()

    async def _migrate_legacy_workspace_tables(self, db: aiosqlite.Connection) -> None:
        """Move old workspace rows into connection-scoped Projects."""
        has_thread_workspaces = await _table_exists(db, "thread_workspaces")
        has_workspace_catalog = await _table_exists(db, "workspace_catalog")
        if has_workspace_catalog:
            rows = await (
                await db.execute(
                    "SELECT root_path, label, updated_at FROM workspace_catalog"
                )
            ).fetchall()
            for root_path, label, updated_at in rows:
                await db.execute(
                    """
                    INSERT INTO projects(
                        project_id, connection_id, root_path, label,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(connection_id, root_path) DO UPDATE SET
                        label=excluded.label,
                        updated_at=excluded.updated_at
                    """,
                    (
                        secrets.token_hex(8),
                        self._default_backend_id,
                        root_path,
                        label,
                        updated_at,
                        updated_at,
                    ),
                )
        if has_thread_workspaces:
            rows = await (await db.execute("""
                    SELECT thread_id, root_path, label, updated_at
                      FROM thread_workspaces
                    """)).fetchall()
            for thread_id, root_path, label, updated_at in rows:
                await db.execute(
                    """
                    INSERT INTO projects(
                        project_id, connection_id, root_path, label,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(connection_id, root_path) DO UPDATE SET
                        label=excluded.label,
                        updated_at=excluded.updated_at
                    """,
                    (
                        secrets.token_hex(8),
                        self._default_backend_id,
                        root_path,
                        label,
                        updated_at,
                        updated_at,
                    ),
                )
                project = await (
                    await db.execute(
                        """
                        SELECT project_id
                          FROM projects
                         WHERE connection_id = ? AND root_path = ?
                        """,
                        (self._default_backend_id, root_path),
                    )
                ).fetchone()
                assert project is not None
                await db.execute(
                    """
                    INSERT INTO thread_projects(thread_id, project_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        project_id=excluded.project_id,
                        updated_at=excluded.updated_at
                    """,
                    (thread_id, project[0], updated_at),
                )
        if has_thread_workspaces:
            await db.execute("DROP TABLE thread_workspaces")
        if has_workspace_catalog:
            await db.execute("DROP TABLE workspace_catalog")

    async def _migrate_legacy_threads_to_anchors_and_bridges(
        self, db: aiosqlite.Connection
    ) -> None:
        """Populate anchor/bridge tables from the older logical-thread table."""
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("""
                SELECT thread_id, chat_key, anchor_id, title, codex_thread_id,
                       codex_backend_id, created_at, updated_at, turn_count,
                       awaiting_reply, interrupted_notice, pending_turn_id,
                       expires_at, closed_at
                  FROM threads
                """)).fetchall()
        bridge_count = await (
            await db.execute("SELECT COUNT(*) FROM bridge_threads")
        ).fetchone()
        if rows and bridge_count is not None and int(bridge_count[0]) == 0:
            for row in rows:
                anchor_id = row["anchor_id"]
                if row["codex_thread_id"] and not anchor_id:
                    anchor_id = await self._ensure_anchor_row(
                        db,
                        chat_key=str(row["chat_key"]),
                        codex_backend_id=str(row["codex_backend_id"]),
                        codex_thread_id=str(row["codex_thread_id"]),
                        title=str(row["title"]),
                        latest_bridge_id=str(row["thread_id"]),
                        created_at=str(row["created_at"]),
                        updated_at=str(row["updated_at"]),
                    )
                await db.execute(
                    """
                    INSERT OR IGNORE INTO bridge_threads(
                        bridge_id, chat_key, anchor_id, title, codex_thread_id,
                        codex_backend_id, created_at, updated_at, turn_count,
                        awaiting_reply, interrupted_notice, pending_turn_id,
                        expires_at, closed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["thread_id"],
                        row["chat_key"],
                        anchor_id,
                        row["title"],
                        row["codex_thread_id"],
                        row["codex_backend_id"],
                        row["created_at"],
                        row["updated_at"],
                        row["turn_count"],
                        row["awaiting_reply"],
                        row["interrupted_notice"],
                        row["pending_turn_id"],
                        row["expires_at"],
                        row["closed_at"],
                    ),
                )
                if anchor_id:
                    await db.execute(
                        """
                        UPDATE threads
                           SET anchor_id = ?
                         WHERE thread_id = ?
                        """,
                        (anchor_id, row["thread_id"]),
                    )
        await db.execute("""
            UPDATE chats
               SET focused_bridge_id = COALESCE(focused_bridge_id, active_thread_id)
            """)
        webhook_rows = await (await db.execute("""
                SELECT webhook_id, thread_id, anchor_id
                  FROM webhook_subscriptions
                """)).fetchall()
        for row in webhook_rows:
            if row["anchor_id"]:
                continue
            bridge = await (
                await db.execute(
                    """
                    SELECT anchor_id
                      FROM bridge_threads
                     WHERE bridge_id = ?
                    """,
                    (row["thread_id"],),
                )
            ).fetchone()
            if bridge is not None and bridge["anchor_id"]:
                await db.execute(
                    """
                    UPDATE webhook_subscriptions
                       SET anchor_id = ?
                     WHERE webhook_id = ?
                    """,
                    (bridge["anchor_id"], row["webhook_id"]),
                )
            else:
                await db.execute(
                    """
                    UPDATE webhook_subscriptions
                       SET enabled = 0,
                           updated_at = ?
                     WHERE webhook_id = ?
                    """,
                    (utcnow(), row["webhook_id"]),
                )

    async def _ensure_anchor_row(
        self,
        db: aiosqlite.Connection,
        *,
        chat_key: str,
        codex_backend_id: str,
        codex_thread_id: str,
        title: str,
        latest_bridge_id: str | None,
        created_at: str,
        updated_at: str,
    ) -> str:
        existing = await (
            await db.execute(
                """
                SELECT anchor_id
                  FROM conversation_anchors
                 WHERE chat_key = ?
                   AND codex_backend_id = ?
                   AND codex_thread_id = ?
                """,
                (chat_key, codex_backend_id, codex_thread_id),
            )
        ).fetchone()
        if existing is not None:
            anchor_id = str(existing["anchor_id"])
            await db.execute(
                """
                UPDATE conversation_anchors
                   SET latest_bridge_id = COALESCE(latest_bridge_id, ?),
                       updated_at = CASE
                           WHEN updated_at < ? THEN ? ELSE updated_at
                       END
                 WHERE anchor_id = ?
                """,
                (latest_bridge_id, updated_at, updated_at, anchor_id),
            )
            return anchor_id
        anchor_id = secrets.token_hex(4)
        await db.execute(
            """
            INSERT INTO conversation_anchors(
                anchor_id, chat_key, codex_backend_id, codex_thread_id, title,
                alias, project_id, latest_bridge_id, broken_reason, archived,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL, 0, ?, ?)
            """,
            (
                anchor_id,
                chat_key,
                codex_backend_id,
                codex_thread_id,
                title,
                latest_bridge_id,
                created_at,
                updated_at,
            ),
        )
        return anchor_id

    async def mark_waiting_threads_interrupted(self) -> None:
        """Mark any waiting threads as interrupted on process startup."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute("""
                UPDATE threads
                   SET awaiting_reply = 0,
                       interrupted_notice = 1,
                       pending_turn_id = NULL
                 WHERE awaiting_reply = 1
                """)
            await db.execute("""
                UPDATE bridge_threads
                   SET awaiting_reply = 0,
                       interrupted_notice = 1,
                       pending_turn_id = NULL
                 WHERE awaiting_reply = 1
                """)
            await db.execute("DELETE FROM pending_requests")
            await db.execute("DELETE FROM pending_user_inputs")
            await db.commit()

    async def ensure_chat(self, chat_key: str) -> None:
        """Ensure the chat record exists."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO chats(chat_key, updated_at)
                VALUES(?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (chat_key, utcnow()),
            )
            await db.commit()

    async def create_thread(
        self,
        chat_key: str,
        thread_id: str,
        title: str,
        *,
        codex_backend_id: str | None = None,
    ) -> None:
        """Create a bridge window and make it focused."""
        await self.create_bridge(
            chat_key=chat_key,
            bridge_id=thread_id,
            title=title,
            anchor_id=None,
            codex_backend_id=codex_backend_id,
            expires_at=None,
            focus=True,
        )

    async def create_bridge(
        self,
        *,
        chat_key: str,
        bridge_id: str,
        title: str,
        anchor_id: str | None,
        codex_backend_id: str | None = None,
        expires_at: str | None = None,
        focus: bool = True,
    ) -> BridgeThread:
        """Create one Telegram bridge window."""
        now = utcnow()
        backend_id = codex_backend_id or self._default_backend_id
        codex_thread_id: str | None = None
        if anchor_id is not None:
            anchor = await self.get_conversation_anchor(anchor_id)
            if anchor is not None:
                codex_thread_id = anchor.codex_thread_id
                backend_id = anchor.codex_backend_id
                title = title or anchor.title
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO bridge_threads(
                    bridge_id, chat_key, anchor_id, title, codex_thread_id,
                    codex_backend_id, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bridge_id,
                    chat_key,
                    anchor_id,
                    title,
                    codex_thread_id,
                    backend_id,
                    now,
                    now,
                    expires_at,
                ),
            )
            await db.execute(
                """
                INSERT INTO threads(
                    thread_id, chat_key, anchor_id, title, codex_thread_id,
                    codex_backend_id, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bridge_id,
                    chat_key,
                    anchor_id,
                    title,
                    codex_thread_id,
                    backend_id,
                    now,
                    now,
                    expires_at,
                ),
            )
            if anchor_id is not None:
                await db.execute(
                    """
                    UPDATE conversation_anchors
                       SET latest_bridge_id = ?,
                           updated_at = ?
                     WHERE anchor_id = ?
                    """,
                    (bridge_id, now, anchor_id),
                )
            if focus:
                await db.execute(
                    """
                    INSERT INTO chats(
                        chat_key, active_thread_id, focused_bridge_id, updated_at
                    ) VALUES(?, ?, ?, ?)
                    ON CONFLICT(chat_key) DO UPDATE SET
                        previous_thread_id = chats.focused_bridge_id,
                        active_thread_id = excluded.active_thread_id,
                        focused_bridge_id = excluded.focused_bridge_id,
                        updated_at = excluded.updated_at
                    """,
                    (chat_key, bridge_id, bridge_id, now),
                )
            await db.commit()
        bridge = await self.get_bridge(bridge_id)
        assert bridge is not None
        return bridge

    async def get_active_thread(self, chat_key: str) -> LogicalThread | None:
        """Return the focused bridge window."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT t.*
                      FROM chats c
                      JOIN threads t ON t.thread_id = c.active_thread_id
                     WHERE c.chat_key = ?
                    """,
                    (chat_key,),
                )
            ).fetchone()
        return _thread_from_row(row) if row is not None else None

    async def get_focused_bridge(self, chat_key: str) -> BridgeThread | None:
        """Return the currently focused bridge window."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT b.*
                      FROM chats c
                      JOIN bridge_threads b ON b.bridge_id = c.focused_bridge_id
                     WHERE c.chat_key = ?
                    """,
                    (chat_key,),
                )
            ).fetchone()
        return _bridge_from_row(row) if row is not None else None

    async def get_bridge(self, bridge_id: str) -> BridgeThread | None:
        """Return one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM bridge_threads WHERE bridge_id = ?",
                    (bridge_id,),
                )
            ).fetchone()
        return _bridge_from_row(row) if row is not None else None

    async def upsert_conversation_anchor(
        self,
        *,
        chat_key: str,
        codex_backend_id: str,
        codex_thread_id: str,
        title: str,
        alias: str | None = None,
        project_id: str | None = None,
        latest_bridge_id: str | None = None,
    ) -> ConversationAnchor:
        """Create or update the durable anchor for one Codex thread."""
        now = utcnow()
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                """
                INSERT INTO conversation_anchors(
                    anchor_id, chat_key, codex_backend_id, codex_thread_id, title,
                    alias, project_id, latest_bridge_id, broken_reason, archived,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
                ON CONFLICT(chat_key, codex_backend_id, codex_thread_id)
                DO UPDATE SET
                    title=excluded.title,
                    alias=COALESCE(excluded.alias, conversation_anchors.alias),
                    project_id=COALESCE(excluded.project_id, conversation_anchors.project_id),
                    latest_bridge_id=COALESCE(
                        excluded.latest_bridge_id,
                        conversation_anchors.latest_bridge_id
                    ),
                    broken_reason=NULL,
                    archived=0,
                    updated_at=excluded.updated_at
                """,
                (
                    secrets.token_hex(4),
                    chat_key,
                    codex_backend_id,
                    codex_thread_id,
                    title,
                    alias,
                    project_id,
                    latest_bridge_id,
                    now,
                    now,
                ),
            )
            row = await (
                await db.execute(
                    """
                    SELECT *
                      FROM conversation_anchors
                     WHERE chat_key = ?
                       AND codex_backend_id = ?
                       AND codex_thread_id = ?
                    """,
                    (chat_key, codex_backend_id, codex_thread_id),
                )
            ).fetchone()
            await db.commit()
        assert row is not None
        return _anchor_from_row(row)

    async def update_conversation_anchor_title(
        self, anchor_id: str, title: str
    ) -> None:
        """Update one anchor title without changing activity ordering."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE conversation_anchors SET title = ? WHERE anchor_id = ?",
                (title, anchor_id),
            )
            await db.commit()

    async def get_conversation_anchor(
        self, anchor_id: str
    ) -> ConversationAnchor | None:
        """Return one durable conversation anchor."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM conversation_anchors WHERE anchor_id = ?",
                    (anchor_id,),
                )
            ).fetchone()
        return _anchor_from_row(row) if row is not None else None

    async def get_conversation_anchor_for_backend_thread(
        self,
        *,
        chat_key: str,
        codex_backend_id: str,
        codex_thread_id: str,
    ) -> ConversationAnchor | None:
        """Return the anchor for one chat/backend thread pair."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT *
                      FROM conversation_anchors
                     WHERE chat_key = ?
                       AND codex_backend_id = ?
                       AND codex_thread_id = ?
                    """,
                    (chat_key, codex_backend_id, codex_thread_id),
                )
            ).fetchone()
        return _anchor_from_row(row) if row is not None else None

    async def list_conversation_anchors(
        self,
        chat_key: str,
    ) -> list[ConversationAnchor]:
        """List durable conversation anchors for one Telegram chat."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT conversation_anchors.*,
                           bridge_threads.pending_turn_id
                               AS latest_bridge_pending_turn_id,
                           bridge_threads.awaiting_reply
                               AS latest_bridge_awaiting_reply,
                           bridge_threads.expires_at
                               AS latest_bridge_expires_at,
                           bridge_threads.closed_at
                               AS latest_bridge_closed_at,
                           EXISTS(
                               SELECT 1
                                 FROM pending_requests
                                WHERE pending_requests.logical_thread_id =
                                      bridge_threads.bridge_id
                           ) AS latest_bridge_pending_approval,
                           EXISTS(
                               SELECT 1
                                 FROM pending_user_inputs
                                WHERE pending_user_inputs.logical_thread_id =
                                      bridge_threads.bridge_id
                           ) AS latest_bridge_pending_user_input
                      FROM conversation_anchors
                      LEFT JOIN bridge_threads
                        ON bridge_threads.bridge_id =
                           conversation_anchors.latest_bridge_id
                     WHERE conversation_anchors.chat_key = ?
                     ORDER BY conversation_anchors.updated_at DESC
                    """,
                    (chat_key,),
                )
            ).fetchall()
        return [_anchor_from_row(row) for row in rows]

    async def list_threads(self, chat_key: str) -> list[LogicalThread]:
        """List all bridge windows for one chat."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT *
                      FROM threads
                     WHERE chat_key = ?
                     ORDER BY updated_at DESC
                    """,
                    (chat_key,),
                )
            ).fetchall()
        return [_thread_from_row(row) for row in rows]

    async def expire_idle_bridges(
        self, *, now: str, focus_expired_before: str
    ) -> list[str]:
        """Clear stale focus and mark expired bridge presentation windows closed."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            focus_rows = await (
                await db.execute(
                    """
                    SELECT b.bridge_id
                      FROM chats c
                      JOIN bridge_threads b
                        ON b.bridge_id = c.focused_bridge_id
                     WHERE b.closed_at IS NULL
                       AND b.awaiting_reply = 0
                       AND b.pending_turn_id IS NULL
                       AND b.updated_at <= ?
                     ORDER BY b.updated_at ASC
                    """,
                    (focus_expired_before,),
                )
            ).fetchall()
            focus_bridge_ids = [str(row["bridge_id"]) for row in focus_rows]
            if focus_bridge_ids:
                placeholders = ",".join("?" for _ in focus_bridge_ids)
                await db.execute(
                    f"""
                    UPDATE chats
                       SET focused_bridge_id = NULL,
                           updated_at = ?
                     WHERE focused_bridge_id IN ({placeholders})
                    """,
                    (now, *focus_bridge_ids),
                )
            rows = await (
                await db.execute(
                    """
                    SELECT bridge_id
                      FROM bridge_threads
                     WHERE closed_at IS NULL
                       AND awaiting_reply = 0
                       AND pending_turn_id IS NULL
                       AND expires_at IS NOT NULL
                       AND expires_at <= ?
                     ORDER BY expires_at ASC
                    """,
                    (now,),
                )
            ).fetchall()
            bridge_ids = [str(row["bridge_id"]) for row in rows]
            if bridge_ids:
                placeholders = ",".join("?" for _ in bridge_ids)
                await db.execute(
                    f"""
                    UPDATE bridge_threads
                       SET closed_at = ?,
                           updated_at = ?
                     WHERE bridge_id IN ({placeholders})
                    """,
                    (now, now, *bridge_ids),
                )
                await db.execute(
                    f"""
                    UPDATE threads
                       SET closed_at = ?,
                           updated_at = ?
                     WHERE thread_id IN ({placeholders})
                    """,
                    (now, now, *bridge_ids),
                )
                await db.execute(
                    f"""
                    UPDATE chats
                       SET focused_bridge_id = NULL,
                           updated_at = ?
                     WHERE focused_bridge_id IN ({placeholders})
                    """,
                    (now, *bridge_ids),
                )
            await db.commit()
        return list(dict.fromkeys([*focus_bridge_ids, *bridge_ids]))

    async def list_interrupted_threads(self) -> list[LogicalThread]:
        """List threads whose last in-flight turn was interrupted by restart."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                    SELECT *
                      FROM threads
                     WHERE interrupted_notice = 1
                     ORDER BY updated_at ASC
                    """)).fetchall()
        return [_thread_from_row(row) for row in rows]

    async def get_thread(self, thread_id: str) -> LogicalThread | None:
        """Return one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM threads WHERE thread_id = ?",
                    (thread_id,),
                )
            ).fetchone()
        return _thread_from_row(row) if row is not None else None

    async def set_active_thread(self, chat_key: str, thread_id: str) -> None:
        """Select one bridge window as active."""
        now = utcnow()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE chats
                   SET previous_thread_id = COALESCE(focused_bridge_id, active_thread_id),
                       active_thread_id = ?,
                       focused_bridge_id = ?,
                       updated_at = ?
                 WHERE chat_key = ?
                """,
                (thread_id, thread_id, now, chat_key),
            )
            await db.execute(
                "UPDATE threads SET updated_at = ? WHERE thread_id = ?",
                (now, thread_id),
            )
            await db.execute(
                "UPDATE bridge_threads SET updated_at = ? WHERE bridge_id = ?",
                (now, thread_id),
            )
            await db.commit()

    async def set_focused_bridge(self, chat_key: str, bridge_id: str) -> None:
        """Select one bridge as the focused Telegram window."""
        await self.set_active_thread(chat_key, bridge_id)

    async def update_codex_thread_binding(
        self,
        thread_id: str,
        codex_thread_id: str,
        *,
        codex_backend_id: str,
    ) -> None:
        """Store the latest bound codex thread id."""
        thread = await self.get_thread(thread_id)
        anchor_id = thread.anchor_id if thread is not None else None
        if thread is not None:
            anchor = await self.upsert_conversation_anchor(
                chat_key=thread.chat_key,
                codex_backend_id=codex_backend_id,
                codex_thread_id=codex_thread_id,
                title=thread.title,
                latest_bridge_id=thread_id,
            )
            anchor_id = anchor.anchor_id
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE threads
                   SET codex_thread_id = ?,
                       codex_backend_id = ?,
                       anchor_id = ?,
                       updated_at = ?
                 WHERE thread_id = ?
                """,
                (codex_thread_id, codex_backend_id, anchor_id, utcnow(), thread_id),
            )
            await db.execute(
                """
                UPDATE bridge_threads
                   SET codex_thread_id = ?,
                       codex_backend_id = ?,
                       anchor_id = ?,
                       updated_at = ?
                 WHERE bridge_id = ?
                """,
                (codex_thread_id, codex_backend_id, anchor_id, utcnow(), thread_id),
            )
            await db.commit()

    async def update_thread_title_if_empty(self, thread_id: str, title: str) -> None:
        """Set the title for placeholder threads only."""
        now = utcnow()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE threads
                   SET title = CASE
                           WHEN title IN ('New thread', 'New conversation') THEN ?
                           ELSE title
                       END,
                       updated_at = ?
                 WHERE thread_id = ?
                """,
                (title, now, thread_id),
            )
            await db.execute(
                """
                UPDATE bridge_threads
                   SET title = CASE
                           WHEN title IN ('New thread', 'New conversation') THEN ?
                           ELSE title
                       END,
                       updated_at = ?
                 WHERE bridge_id = ?
                """,
                (title, now, thread_id),
            )
            await db.execute(
                """
                UPDATE conversation_anchors
                   SET title = ?
                 WHERE anchor_id = (
                       SELECT anchor_id
                         FROM threads
                        WHERE thread_id = ?
                   )
                   AND title IN ('New thread', 'New conversation')
                """,
                (title, thread_id),
            )
            await db.commit()

    async def mark_turn_started(self, thread_id: str, turn_id: str) -> None:
        """Mark one bridge window as awaiting a backend reply."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE threads
                   SET awaiting_reply = 1,
                       interrupted_notice = 0,
                       pending_turn_id = ?,
                       updated_at = ?
                 WHERE thread_id = ?
                """,
                (turn_id, utcnow(), thread_id),
            )
            await db.execute(
                """
                UPDATE bridge_threads
                   SET awaiting_reply = 1,
                       interrupted_notice = 0,
                       pending_turn_id = ?,
                       updated_at = ?
                 WHERE bridge_id = ?
                """,
                (turn_id, utcnow(), thread_id),
            )
            await db.commit()

    async def mark_turn_completed(self, thread_id: str) -> None:
        """Clear waiting state and increment turn counters."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE threads
                   SET awaiting_reply = 0,
                       pending_turn_id = NULL,
                       turn_count = turn_count + 1,
                       updated_at = ?
                 WHERE thread_id = ?
                """,
                (utcnow(), thread_id),
            )
            await db.execute(
                """
                UPDATE bridge_threads
                   SET awaiting_reply = 0,
                       pending_turn_id = NULL,
                       turn_count = turn_count + 1,
                       updated_at = ?
                 WHERE bridge_id = ?
                """,
                (utcnow(), thread_id),
            )
            await db.commit()

    async def mark_turn_failed(self, thread_id: str) -> None:
        """Clear waiting state after a backend boundary failure."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE threads
                   SET awaiting_reply = 0,
                       pending_turn_id = NULL,
                       updated_at = ?
                 WHERE thread_id = ?
                """,
                (utcnow(), thread_id),
            )
            await db.execute(
                """
                UPDATE bridge_threads
                   SET awaiting_reply = 0,
                       pending_turn_id = NULL,
                       updated_at = ?
                 WHERE bridge_id = ?
                """,
                (utcnow(), thread_id),
            )
            await db.commit()

    async def take_interrupted_notice(self, thread_id: str) -> bool:
        """Return and clear the interrupted notice flag."""
        async with aiosqlite.connect(self._path) as db:
            row = await (
                await db.execute(
                    "SELECT interrupted_notice FROM threads WHERE thread_id = ?",
                    (thread_id,),
                )
            ).fetchone()
            if row is None or not bool(row[0]):
                return False
            await db.execute(
                "UPDATE threads SET interrupted_notice = 0 WHERE thread_id = ?",
                (thread_id,),
            )
            await db.commit()
            return True

    async def get_overrides(self, thread_id: str) -> SessionOverrides:
        """Load session overrides for one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM overrides WHERE thread_id = ?",
                    (thread_id,),
                )
            ).fetchone()
        if row is None:
            return SessionOverrides()
        return SessionOverrides(
            profile=row["profile"],
            model=row["model"],
            effort=row["effort"],
            summary=row["summary"],
            cwd=row["cwd"],
            fast_mode=(
                bool(row["fast_mode"])
                if "fast_mode_is_set" in row.keys() and bool(row["fast_mode_is_set"])
                else None
            ),
            verbosity=row["verbosity"],
            command_verbosity=row["command_verbosity"],
            followup_mode=row["followup_mode"],
            collaboration_mode=(
                row["collaboration_mode"]
                if "collaboration_mode" in row.keys()
                else None
            ),
        )

    async def upsert_overrides(
        self, thread_id: str, overrides: SessionOverrides
    ) -> SessionOverrides:
        """Replace session overrides for one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO overrides(
                    thread_id, profile, model, effort, summary, cwd, fast_mode,
                    fast_mode_is_set, verbosity, command_verbosity, followup_mode,
                    collaboration_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    profile=excluded.profile,
                    model=excluded.model,
                    effort=excluded.effort,
                    summary=excluded.summary,
                    cwd=excluded.cwd,
                    fast_mode=excluded.fast_mode,
                    fast_mode_is_set=excluded.fast_mode_is_set,
                    verbosity=excluded.verbosity,
                    command_verbosity=excluded.command_verbosity,
                    followup_mode=excluded.followup_mode,
                    collaboration_mode=excluded.collaboration_mode
                """,
                (
                    thread_id,
                    overrides.profile,
                    overrides.model,
                    overrides.effort,
                    overrides.summary,
                    overrides.cwd,
                    (
                        int(overrides.fast_mode)
                        if overrides.fast_mode is not None
                        else None
                    ),
                    int(overrides.fast_mode is not None),
                    overrides.verbosity,
                    overrides.command_verbosity,
                    overrides.followup_mode,
                    overrides.collaboration_mode,
                ),
            )
            await db.commit()
        return overrides

    async def clear_overrides(self, thread_id: str) -> None:
        """Clear all overrides for one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM overrides WHERE thread_id = ?", (thread_id,))
            await db.commit()

    async def add_thread_message(
        self,
        thread_id: str,
        *,
        role: str,
        kind: str,
        text: str,
        turn_id: str | None = None,
    ) -> None:
        """Append one compact transcript entry and trim old rows."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO thread_messages(
                    thread_id, role, kind, text, created_at, turn_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (thread_id, role, kind, text, utcnow(), turn_id),
            )
            await db.execute(
                """
                DELETE FROM thread_messages
                 WHERE thread_id = ?
                   AND id NOT IN (
                        SELECT id
                          FROM thread_messages
                         WHERE thread_id = ?
                         ORDER BY id DESC
                         LIMIT ?
                   )
                """,
                (thread_id, thread_id, THREAD_MESSAGE_LIMIT),
            )
            await db.commit()

    async def list_thread_messages(
        self,
        thread_id: str,
        *,
        limit: int = 10,
    ) -> list[ThreadMessage]:
        """Return recent transcript entries ordered oldest to newest."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT id, thread_id, role, kind, text, created_at, turn_id
                      FROM thread_messages
                     WHERE thread_id = ?
                     ORDER BY id DESC
                     LIMIT ?
                    """,
                    (thread_id, max(limit, 1)),
                )
            ).fetchall()
        return [_thread_message_from_row(row) for row in reversed(list(rows))]

    async def list_undelivered_final_thread_messages(
        self,
        *,
        chat_key: str,
        anchor_id: str,
        thread_id: str,
        limit: int = 20,
    ) -> list[ThreadMessage]:
        """Return assistant final transcript entries after the delivery watermark."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT id, thread_id, role, kind, text, created_at, turn_id
                      FROM thread_messages
                     WHERE thread_id = ?
                       AND role = 'assistant'
                       AND kind IN ('final', 'final_image')
                       AND id > COALESCE(
                            (
                                SELECT last_message_id
                                  FROM thread_delivery_watermarks
                                 WHERE chat_key = ?
                                   AND anchor_id = ?
                                   AND thread_id = ?
                            ),
                            0
                       )
                     ORDER BY id ASC
                     LIMIT ?
                    """,
                    (thread_id, chat_key, anchor_id, thread_id, max(limit, 1)),
                )
            ).fetchall()
        return [_thread_message_from_row(row) for row in rows]

    async def get_latest_final_thread_message(
        self,
        thread_id: str,
    ) -> ThreadMessage | None:
        """Return the newest assistant final transcript entry for one bridge."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT id, thread_id, role, kind, text, created_at, turn_id
                      FROM thread_messages
                     WHERE thread_id = ?
                       AND role = 'assistant'
                       AND kind = 'final'
                     ORDER BY id DESC
                     LIMIT 1
                    """,
                    (thread_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return _thread_message_from_row(row)

    async def mark_thread_messages_delivered(
        self,
        *,
        chat_key: str,
        anchor_id: str,
        thread_id: str,
    ) -> None:
        """Advance the delivered watermark through the newest local transcript row."""
        async with aiosqlite.connect(self._path) as db:
            row = await (
                await db.execute(
                    "SELECT MAX(id) FROM thread_messages WHERE thread_id = ?",
                    (thread_id,),
                )
            ).fetchone()
            latest_id = int(row[0]) if row is not None and row[0] is not None else 0
            await db.execute(
                """
                INSERT INTO thread_delivery_watermarks(
                    chat_key, anchor_id, thread_id, last_message_id, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_key, anchor_id, thread_id) DO UPDATE SET
                    last_message_id = MAX(
                        thread_delivery_watermarks.last_message_id,
                        excluded.last_message_id
                    ),
                    updated_at=excluded.updated_at
                """,
                (chat_key, anchor_id, thread_id, latest_id, utcnow()),
            )
            await db.commit()

    async def remember_directory(self, thread_id: str, path: str) -> None:
        """Record one selected working directory, keeping the newest copy."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM thread_directories WHERE thread_id = ? AND path = ?",
                (thread_id, path),
            )
            await db.execute(
                """
                INSERT INTO thread_directories(thread_id, path, selected_at)
                VALUES(?, ?, ?)
                """,
                (thread_id, path, utcnow()),
            )
            await db.commit()

    async def list_directories(
        self,
        thread_id: str,
        *,
        limit: int = 10,
    ) -> list[DirectoryEntry]:
        """Return recent directory selections ordered newest first."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT id, thread_id, path, selected_at
                      FROM thread_directories
                     WHERE thread_id = ?
                     ORDER BY id DESC
                     LIMIT ?
                    """,
                    (thread_id, max(limit, 1)),
                )
            ).fetchall()
        return [_directory_entry_from_row(row) for row in rows]

    async def enqueue_attachment_job(
        self,
        thread_id: str,
        path: str,
        *,
        caption: str | None = None,
    ) -> AttachmentJob:
        """Queue one outbound Telegram attachment delivery."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Unknown thread: {thread_id}")
        now = utcnow()
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                INSERT INTO attachment_jobs(
                    chat_key,
                    logical_thread_id,
                    path,
                    caption,
                    status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (thread.chat_key, thread_id, path, caption, now, now),
            )
            await db.commit()
            job_id = cursor.lastrowid
        return AttachmentJob(
            job_id=job_id,
            chat_key=thread.chat_key,
            logical_thread_id=thread_id,
            path=path,
            caption=caption,
            status="pending",
            created_at=now,
            updated_at=now,
        )

    async def list_pending_attachment_jobs(
        self,
        *,
        limit: int = 20,
    ) -> list[AttachmentJob]:
        """Return pending outbound attachment jobs in FIFO order."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT *
                      FROM attachment_jobs
                     WHERE status = 'pending'
                     ORDER BY id ASC
                     LIMIT ?
                    """,
                    (max(limit, 1),),
                )
            ).fetchall()
        return [_attachment_job_from_row(row) for row in rows]

    async def mark_attachment_job_delivered(self, job_id: int) -> None:
        """Mark one outbound attachment as delivered."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE attachment_jobs
                   SET status = 'delivered',
                       error = NULL,
                       updated_at = ?
                 WHERE id = ?
                """,
                (utcnow(), job_id),
            )
            await db.commit()

    async def mark_attachment_job_failed(self, job_id: int, error: str) -> None:
        """Mark one outbound attachment as failed."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE attachment_jobs
                   SET status = 'failed',
                       error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (error, utcnow(), job_id),
            )
            await db.commit()

    async def enqueue_bridge_control_job(
        self,
        thread_id: str,
        kind: str,
        payload: dict[str, object],
    ) -> BridgeControlJob:
        """Queue one Telegram bridge-control delivery request."""
        thread = await self.get_thread(thread_id)
        if thread is None:
            raise ValueError(f"Unknown thread: {thread_id}")
        now = utcnow()
        payload_json = json.dumps(payload, sort_keys=True)
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                INSERT INTO bridge_control_jobs(
                    chat_key,
                    logical_thread_id,
                    kind,
                    payload_json,
                    status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (thread.chat_key, thread_id, kind, payload_json, now, now),
            )
            await db.commit()
            job_id = cursor.lastrowid
        return BridgeControlJob(
            job_id=job_id,
            chat_key=thread.chat_key,
            logical_thread_id=thread_id,
            kind=kind,
            payload=dict(payload),
            status="pending",
            created_at=now,
            updated_at=now,
        )

    async def list_pending_bridge_control_jobs(
        self,
        *,
        limit: int = 20,
    ) -> list[BridgeControlJob]:
        """Return pending bridge-control jobs in FIFO order."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT *
                      FROM bridge_control_jobs
                     WHERE status = 'pending'
                     ORDER BY id ASC
                     LIMIT ?
                    """,
                    (max(limit, 1),),
                )
            ).fetchall()
        return [_bridge_control_job_from_row(row) for row in rows]

    async def mark_bridge_control_job_delivered(self, job_id: int) -> None:
        """Mark one bridge-control job as delivered."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE bridge_control_jobs
                   SET status = 'delivered',
                       error = NULL,
                       updated_at = ?
                 WHERE id = ?
                """,
                (utcnow(), job_id),
            )
            await db.commit()

    async def mark_bridge_control_job_failed(self, job_id: int, error: str) -> None:
        """Mark one bridge-control job as failed."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE bridge_control_jobs
                   SET status = 'failed',
                       error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (error, utcnow(), job_id),
            )
            await db.commit()

    async def upsert_project(
        self,
        *,
        connection_id: str,
        root_path: str,
        label: str,
    ) -> Project:
        """Create or update one connection-scoped Project."""
        now = utcnow()
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            existing = await (
                await db.execute(
                    """
                    SELECT *
                      FROM projects
                     WHERE connection_id = ? AND root_path = ?
                    """,
                    (connection_id, root_path),
                )
            ).fetchone()
            project_id = (
                str(existing["project_id"])
                if existing is not None
                else secrets.token_hex(8)
            )
            await db.execute(
                """
                INSERT INTO projects(
                    project_id, connection_id, root_path, label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(connection_id, root_path) DO UPDATE SET
                    label=excluded.label,
                    updated_at=excluded.updated_at
                """,
                (project_id, connection_id, root_path, label, now, now),
            )
            await db.commit()
            row = await (
                await db.execute(
                    "SELECT * FROM projects WHERE project_id = ?", (project_id,)
                )
            ).fetchone()
        assert row is not None
        return _project_from_row(row)

    async def list_projects(
        self,
        *,
        connection_id: str | None = None,
        limit: int = 50,
    ) -> list[Project]:
        """Return known Projects ordered by recency."""
        where = ""
        params: list[object] = []
        if connection_id is not None:
            where = "WHERE connection_id = ?"
            params.append(connection_id)
        params.append(max(limit, 1))
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"""
                    SELECT *
                      FROM projects
                      {where}
                     ORDER BY updated_at DESC, label ASC
                     LIMIT ?
                    """,
                    params,
                )
            ).fetchall()
        return [_project_from_row(row) for row in rows]

    async def get_project(self, project_id: str) -> Project | None:
        """Return one Project by id."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM projects WHERE project_id = ?",
                    (project_id,),
                )
            ).fetchone()
        return _project_from_row(row) if row is not None else None

    async def bind_thread_project(self, thread_id: str, project_id: str) -> None:
        """Bind one bridge window to a Project."""
        now = utcnow()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO thread_projects(thread_id, project_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    project_id=excluded.project_id,
                    updated_at=excluded.updated_at
                """,
                (thread_id, project_id, now),
            )
            project = await (
                await db.execute(
                    "SELECT connection_id FROM projects WHERE project_id = ?",
                    (project_id,),
                )
            ).fetchone()
            if project is not None:
                await db.execute(
                    """
                    UPDATE threads
                       SET codex_backend_id = ?,
                           updated_at = ?
                     WHERE thread_id = ?
                    """,
                    (project[0], now, thread_id),
                )
            await db.commit()

    async def get_thread_project(self, thread_id: str) -> Project | None:
        """Return the Project bound to one thread."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT projects.*
                      FROM thread_projects
                      JOIN projects ON projects.project_id = thread_projects.project_id
                     WHERE thread_projects.thread_id = ?
                    """,
                    (thread_id,),
                )
            ).fetchone()
        return _project_from_row(row) if row is not None else None

    async def clear_thread_project(self, thread_id: str) -> None:
        """Remove the active Project binding for one thread."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM thread_projects WHERE thread_id = ?",
                (thread_id,),
            )
            await db.commit()

    async def get_project_overrides(self, project_id: str) -> SessionOverrides:
        """Load project-scoped runtime overrides."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT model, effort, fast_mode, fast_mode_is_set
                      FROM project_overrides
                     WHERE project_id = ?
                    """,
                    (project_id,),
                )
            ).fetchone()
        if row is None:
            return SessionOverrides()
        return SessionOverrides(
            model=row["model"],
            effort=row["effort"],
            fast_mode=bool(row["fast_mode"]) if bool(row["fast_mode_is_set"]) else None,
        )

    async def upsert_project_overrides(
        self,
        project_id: str,
        overrides: SessionOverrides,
    ) -> SessionOverrides:
        """Replace project-scoped runtime defaults."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO project_overrides(
                    project_id, model, effort, fast_mode, fast_mode_is_set, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    model=excluded.model,
                    effort=excluded.effort,
                    fast_mode=excluded.fast_mode,
                    fast_mode_is_set=excluded.fast_mode_is_set,
                    updated_at=excluded.updated_at
                """,
                (
                    project_id,
                    overrides.model,
                    overrides.effort,
                    (
                        int(overrides.fast_mode)
                        if overrides.fast_mode is not None
                        else None
                    ),
                    int(overrides.fast_mode is not None),
                    utcnow(),
                ),
            )
            await db.commit()
        return SessionOverrides(
            model=overrides.model,
            effort=overrides.effort,
            fast_mode=overrides.fast_mode,
        )

    async def create_webhook_subscription(
        self,
        *,
        webhook_id: str,
        chat_key: str,
        anchor_id: str | None = None,
        thread_id: str | None = None,
        name: str,
        secret_hash: str,
    ) -> WebhookSubscription:
        """Persist one durable external-event subscription."""
        now = utcnow()
        if anchor_id is None and thread_id is not None:
            bridge = await self.get_bridge(thread_id)
            anchor_id = bridge.anchor_id if bridge is not None else None
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO webhook_subscriptions(
                    webhook_id,
                    chat_key,
                    thread_id,
                    anchor_id,
                    name,
                    secret_hash,
                    enabled,
                    created_at,
                    updated_at,
                    trigger_count,
                    last_triggered_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 0, NULL)
                """,
                (
                    webhook_id,
                    chat_key,
                    thread_id,
                    anchor_id,
                    name,
                    secret_hash,
                    now,
                    now,
                ),
            )
            await db.commit()
        subscription = await self.get_webhook_subscription(webhook_id)
        assert subscription is not None
        return subscription

    async def list_webhook_subscriptions(
        self,
        *,
        chat_key: str | None = None,
        thread_id: str | None = None,
        anchor_id: str | None = None,
        include_disabled: bool = False,
    ) -> list[WebhookSubscription]:
        """List webhook subscriptions with optional chat/thread filtering."""
        clauses: list[str] = []
        values: list[object] = []
        if chat_key is not None:
            clauses.append("webhook_subscriptions.chat_key = ?")
            values.append(chat_key)
        if thread_id is not None:
            clauses.append("webhook_subscriptions.thread_id = ?")
            values.append(thread_id)
        if anchor_id is not None:
            clauses.append("webhook_subscriptions.anchor_id = ?")
            values.append(anchor_id)
        if not include_disabled:
            clauses.append("webhook_subscriptions.enabled = 1")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    f"""
                    SELECT webhook_subscriptions.*,
                           conversation_anchors.codex_backend_id AS codex_backend_id,
                           conversation_anchors.codex_thread_id AS codex_thread_id,
                           conversation_anchors.latest_bridge_id AS latest_bridge_id
                      FROM webhook_subscriptions
                      LEFT JOIN conversation_anchors
                        ON conversation_anchors.anchor_id = webhook_subscriptions.anchor_id
                      {where}
                     ORDER BY webhook_subscriptions.updated_at DESC,
                              webhook_subscriptions.created_at DESC
                    """,
                    tuple(values),
                )
            ).fetchall()
        return [_webhook_subscription_from_row(row) for row in rows]

    async def get_webhook_subscription(
        self,
        webhook_id: str,
    ) -> WebhookSubscription | None:
        """Return one webhook subscription."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT webhook_subscriptions.*,
                           conversation_anchors.codex_backend_id AS codex_backend_id,
                           conversation_anchors.codex_thread_id AS codex_thread_id,
                           conversation_anchors.latest_bridge_id AS latest_bridge_id
                      FROM webhook_subscriptions
                      LEFT JOIN conversation_anchors
                        ON conversation_anchors.anchor_id = webhook_subscriptions.anchor_id
                     WHERE webhook_id = ?
                    """,
                    (webhook_id,),
                )
            ).fetchone()
        return _webhook_subscription_from_row(row) if row is not None else None

    async def get_webhook_secret_hash(self, webhook_id: str) -> str | None:
        """Return the stored event-secret hash for one subscription."""
        async with aiosqlite.connect(self._path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT secret_hash
                      FROM webhook_subscriptions
                     WHERE webhook_id = ?
                    """,
                    (webhook_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return str(row[0])

    async def disable_webhook_subscription(self, webhook_id: str) -> bool:
        """Disable one webhook subscription."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                UPDATE webhook_subscriptions
                   SET enabled = 0,
                       updated_at = ?
                 WHERE webhook_id = ?
                   AND enabled = 1
                """,
                (utcnow(), webhook_id),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def record_webhook_delivery(
        self,
        webhook_id: str,
        *,
        idempotency_key: str | None,
    ) -> bool:
        """Record one event idempotency key, returning false for duplicates."""
        if idempotency_key is None:
            return True
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO webhook_deliveries(
                    webhook_id,
                    idempotency_key,
                    created_at
                ) VALUES (?, ?, ?)
                """,
                (webhook_id, idempotency_key, utcnow()),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def mark_webhook_triggered(self, webhook_id: str) -> None:
        """Increment trigger counters for one subscription."""
        now = utcnow()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE webhook_subscriptions
                   SET trigger_count = trigger_count + 1,
                       last_triggered_at = ?,
                       updated_at = ?
                 WHERE webhook_id = ?
                """,
                (now, now, webhook_id),
            )
            await db.commit()

    async def create_callback_token(
        self,
        *,
        chat_key: str,
        topic_id: int | None,
        action: str,
        payload: dict[str, object],
        expires_at: str,
    ) -> str:
        """Create one short-lived Telegram callback token."""
        token = secrets.token_hex(4)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO callback_tokens(
                    token,
                    chat_key,
                    topic_id,
                    action,
                    payload_json,
                    expires_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    chat_key,
                    topic_id,
                    action,
                    json.dumps(payload, sort_keys=True),
                    expires_at,
                    utcnow(),
                ),
            )
            await db.commit()
        return token

    async def consume_callback_token(
        self,
        token: str,
        *,
        chat_key: str,
        topic_id: int | None,
    ) -> CallbackToken | None:
        """Consume one valid callback token for this chat/topic."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM callback_tokens WHERE token = ?",
                    (token,),
                )
            ).fetchone()
            if row is None:
                return None
            expired = _is_expired(str(row["expires_at"]))
            if (
                str(row["chat_key"]) != chat_key
                or row["topic_id"] != topic_id
                or expired
            ):
                if expired:
                    await db.execute(
                        "DELETE FROM callback_tokens WHERE token = ?",
                        (token,),
                    )
                    await db.commit()
                return None
            await db.execute("DELETE FROM callback_tokens WHERE token = ?", (token,))
            await db.commit()
        return _callback_token_from_row(row)

    async def add_pending_request(self, request: PendingApproval) -> None:
        """Persist one pending approval request."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO pending_requests(
                    request_id, chat_key, logical_thread_id, codex_thread_id,
                    codex_backend_id, turn_id, method, command_text, reason,
                    approval_message, raw_params, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.chat_key,
                    request.logical_thread_id,
                    request.codex_thread_id,
                    request.codex_backend_id,
                    request.turn_id,
                    request.method,
                    request.command,
                    request.reason,
                    request.message,
                    json.dumps(request.raw_params),
                    utcnow(),
                ),
            )
            await db.commit()

    async def get_pending_request(self, chat_key: str) -> PendingApproval | None:
        """Return the oldest pending approval for one chat."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT *
                      FROM pending_requests
                     WHERE chat_key = ?
                     ORDER BY request_id ASC
                     LIMIT 1
                    """,
                    (chat_key,),
                )
            ).fetchone()
        return _pending_from_row(row) if row is not None else None

    async def pop_pending_request(self, request_id: int) -> PendingApproval | None:
        """Remove and return one pending approval request."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM pending_requests WHERE request_id = ?",
                    (request_id,),
                )
            ).fetchone()
            if row is None:
                return None
            await db.execute(
                "DELETE FROM pending_requests WHERE request_id = ?",
                (request_id,),
            )
            await db.commit()
        return _pending_from_row(row)

    async def clear_pending_for_thread(self, thread_id: str) -> None:
        """Clear all pending approvals for one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM pending_requests WHERE logical_thread_id = ?",
                (thread_id,),
            )
            await db.commit()

    async def add_pending_user_input(self, request: PendingUserInput) -> None:
        """Persist one pending user-input request."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO pending_user_inputs(
                    request_id,
                    chat_key,
                    logical_thread_id,
                    codex_thread_id,
                    codex_backend_id,
                    turn_id,
                    method,
                    questions_json,
                    selected_answers_json,
                    awaiting_free_text_question_id,
                    raw_params,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.chat_key,
                    request.logical_thread_id,
                    request.codex_thread_id,
                    request.codex_backend_id,
                    request.turn_id,
                    request.method,
                    _questions_to_json(request.questions),
                    _answers_to_json(request.selected_answers),
                    request.awaiting_free_text_question_id,
                    json.dumps(request.raw_params),
                    utcnow(),
                ),
            )
            await db.commit()

    async def get_pending_user_input(self, chat_key: str) -> PendingUserInput | None:
        """Return the oldest pending user-input request for one chat."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT *
                      FROM pending_user_inputs
                     WHERE chat_key = ?
                     ORDER BY request_id ASC
                     LIMIT 1
                    """,
                    (chat_key,),
                )
            ).fetchone()
        return _pending_user_input_from_row(row) if row is not None else None

    async def update_pending_user_input_selection(
        self,
        request_id: int,
        *,
        question_id: str,
        answers: tuple[str, ...],
        awaiting_free_text: bool,
    ) -> None:
        """Update one answer selection for a pending user-input request."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM pending_user_inputs WHERE request_id = ?",
                    (request_id,),
                )
            ).fetchone()
            if row is None:
                return
            selected = _answers_from_json(str(row["selected_answers_json"]))
            if answers:
                selected[question_id] = answers
            else:
                selected.pop(question_id, None)
            await db.execute(
                """
                UPDATE pending_user_inputs
                   SET selected_answers_json = ?,
                       awaiting_free_text_question_id = ?
                 WHERE request_id = ?
                """,
                (
                    _answers_to_json(selected),
                    question_id if awaiting_free_text else None,
                    request_id,
                ),
            )
            await db.commit()

    async def pop_pending_user_input(self, request_id: int) -> PendingUserInput | None:
        """Remove and return one pending user-input request."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM pending_user_inputs WHERE request_id = ?",
                    (request_id,),
                )
            ).fetchone()
            if row is None:
                return None
            await db.execute(
                "DELETE FROM pending_user_inputs WHERE request_id = ?",
                (request_id,),
            )
            await db.commit()
        return _pending_user_input_from_row(row)

    async def clear_pending_user_input_for_thread(self, thread_id: str) -> None:
        """Clear all pending user-input requests for one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM pending_user_inputs WHERE logical_thread_id = ?",
                (thread_id,),
            )
            await db.commit()


class SQLiteTelegramProgressStore:
    """Durable Telegram rendering state."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def initialize(self) -> None:
        """Create the progress bookkeeping table if missing."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_progress (
                    logical_thread_id TEXT PRIMARY KEY,
                    message_id INTEGER,
                    rendered_text TEXT,
                    updated_at TEXT NOT NULL
                )
                """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_final_messages (
                    logical_thread_id TEXT PRIMARY KEY,
                    chat_key TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    rendered_text TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_telegram_final_messages_reply
                    ON telegram_final_messages(chat_key, message_id)
                """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_pending_reply_targets (
                    chat_key TEXT NOT NULL,
                    prompt_message_id INTEGER NOT NULL,
                    target_thread_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(chat_key, prompt_message_id)
                )
                """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_telegram_pending_reply_targets_expires
                    ON telegram_pending_reply_targets(expires_at)
                """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_status_cards (
                    chat_key TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    topic_id INTEGER,
                    message_id INTEGER NOT NULL,
                    rendered_text TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            await db.commit()

    async def get_progress(self, thread_id: str) -> ProgressMessageState | None:
        """Load Telegram progress state for one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT message_id, rendered_text
                      FROM telegram_progress
                     WHERE logical_thread_id = ?
                    """,
                    (thread_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return ProgressMessageState(
            message_id=row["message_id"],
            rendered_text=row["rendered_text"],
        )

    async def save_progress(
        self,
        thread_id: str,
        *,
        message_id: int | None = None,
        rendered_text: str | None = None,
    ) -> None:
        """Persist Telegram progress rendering metadata."""
        async with aiosqlite.connect(self._path) as db:
            current = await (
                await db.execute(
                    """
                    SELECT message_id, rendered_text
                      FROM telegram_progress
                     WHERE logical_thread_id = ?
                    """,
                    (thread_id,),
                )
            ).fetchone()
            resolved_message_id = (
                message_id
                if message_id is not None
                else (current[0] if current is not None else None)
            )
            resolved_text = (
                rendered_text
                if rendered_text is not None
                else (current[1] if current is not None else None)
            )
            await db.execute(
                """
                INSERT INTO telegram_progress(
                    logical_thread_id, message_id, rendered_text, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(logical_thread_id) DO UPDATE SET
                    message_id=excluded.message_id,
                    rendered_text=excluded.rendered_text,
                    updated_at=excluded.updated_at
                """,
                (thread_id, resolved_message_id, resolved_text, utcnow()),
            )
            await db.commit()

    async def clear_progress(self, thread_id: str) -> None:
        """Clear progress message bookkeeping."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM telegram_progress WHERE logical_thread_id = ?",
                (thread_id,),
            )
            await db.commit()

    async def save_final_message(
        self,
        thread_id: str,
        *,
        chat_key: str,
        message_id: int,
        rendered_text: str,
    ) -> None:
        """Persist the Telegram final response message for reply routing."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO telegram_final_messages(
                    logical_thread_id, chat_key, message_id, rendered_text, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(logical_thread_id) DO UPDATE SET
                    chat_key=excluded.chat_key,
                    message_id=excluded.message_id,
                    rendered_text=excluded.rendered_text,
                    updated_at=excluded.updated_at
                """,
                (thread_id, chat_key, message_id, rendered_text, utcnow()),
            )
            await db.commit()

    async def get_final_message(
        self,
        thread_id: str,
    ) -> FinalMessageState | None:
        """Load the final Telegram message mapped to one bridge window."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT logical_thread_id, chat_key, message_id, rendered_text
                      FROM telegram_final_messages
                     WHERE logical_thread_id = ?
                    """,
                    (thread_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return _final_message_from_row(row)

    async def get_final_message_by_reply(
        self,
        chat_key: str,
        message_id: int,
    ) -> FinalMessageState | None:
        """Resolve a Telegram reply target to a bridge window."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT logical_thread_id, chat_key, message_id, rendered_text
                      FROM telegram_final_messages
                     WHERE chat_key = ?
                       AND message_id = ?
                    """,
                    (chat_key, message_id),
                )
            ).fetchone()
        if row is None:
            return None
        return _final_message_from_row(row)

    async def save_pending_reply_target(
        self,
        *,
        chat_key: str,
        prompt_message_id: int,
        target_thread_id: str,
        expires_at: str,
    ) -> None:
        """Persist one ForceReply prompt target."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO telegram_pending_reply_targets(
                    chat_key, prompt_message_id, target_thread_id, expires_at,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_key, prompt_message_id) DO UPDATE SET
                    target_thread_id=excluded.target_thread_id,
                    expires_at=excluded.expires_at
                """,
                (chat_key, prompt_message_id, target_thread_id, expires_at, utcnow()),
            )
            await db.commit()

    async def consume_pending_reply_target(
        self,
        *,
        chat_key: str,
        prompt_message_id: int,
    ) -> PendingReplyTarget | None:
        """Consume one ForceReply prompt target if it has not expired."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT chat_key, prompt_message_id, target_thread_id, expires_at
                      FROM telegram_pending_reply_targets
                     WHERE chat_key = ?
                       AND prompt_message_id = ?
                    """,
                    (chat_key, prompt_message_id),
                )
            ).fetchone()
            await db.execute(
                """
                DELETE FROM telegram_pending_reply_targets
                 WHERE chat_key = ?
                   AND prompt_message_id = ?
                """,
                (chat_key, prompt_message_id),
            )
            await db.execute(
                "DELETE FROM telegram_pending_reply_targets WHERE expires_at <= ?",
                (utcnow(),),
            )
            await db.commit()
        if row is None:
            return None
        if _is_expired(str(row["expires_at"])):
            return None
        return PendingReplyTarget(
            chat_key=row["chat_key"],
            prompt_message_id=row["prompt_message_id"],
            target_thread_id=row["target_thread_id"],
        )

    async def get_status_card(self, chat_key: str) -> StatusCardState | None:
        """Load the sticky Telegram status card for one chat."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT chat_key, chat_id, topic_id, message_id, rendered_text,
                           updated_at
                      FROM telegram_status_cards
                     WHERE chat_key = ?
                    """,
                    (chat_key,),
                )
            ).fetchone()
        if row is None:
            return None
        return StatusCardState(
            chat_key=str(row["chat_key"]),
            chat_id=int(row["chat_id"]),
            topic_id=row["topic_id"],
            message_id=int(row["message_id"]),
            rendered_text=str(row["rendered_text"]),
            updated_at=str(row["updated_at"]),
        )

    async def save_status_card(
        self,
        chat_key: str,
        *,
        chat_id: int,
        topic_id: int | None,
        message_id: int,
        rendered_text: str,
    ) -> None:
        """Persist the sticky Telegram status card for one chat."""
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO telegram_status_cards(
                    chat_key, chat_id, topic_id, message_id, rendered_text, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    topic_id=excluded.topic_id,
                    message_id=excluded.message_id,
                    rendered_text=excluded.rendered_text,
                    updated_at=excluded.updated_at
                """,
                (chat_key, chat_id, topic_id, message_id, rendered_text, utcnow()),
            )
            await db.commit()


async def _ensure_column(
    db: aiosqlite.Connection,
    *,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    rows = await (await db.execute(f"PRAGMA table_info({table_name})")).fetchall()
    if any(str(row[1]) == column_name for row in rows):
        return
    await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


async def _ensure_overrides_fast_mode_nullable(db: aiosqlite.Connection) -> None:
    rows = await (await db.execute("PRAGMA table_info(overrides)")).fetchall()
    fast_mode = next((row for row in rows if str(row[1]) == "fast_mode"), None)
    if fast_mode is None or not int(fast_mode[3]):
        return

    await db.execute("ALTER TABLE overrides RENAME TO overrides_legacy_fast_mode")
    await db.execute("""
        CREATE TABLE overrides (
            thread_id TEXT PRIMARY KEY,
            profile TEXT,
            model TEXT,
            effort TEXT,
            summary TEXT,
            cwd TEXT,
            fast_mode INTEGER,
            fast_mode_is_set INTEGER NOT NULL DEFAULT 0,
            verbosity TEXT,
            command_verbosity TEXT,
            followup_mode TEXT,
            collaboration_mode TEXT
        )
    """)
    await db.execute("""
        INSERT INTO overrides(
            thread_id, profile, model, effort, summary, cwd, fast_mode,
            fast_mode_is_set, verbosity, command_verbosity, followup_mode,
            collaboration_mode
        )
        SELECT thread_id, profile, model, effort, summary, cwd, fast_mode,
               CASE WHEN fast_mode IS NOT NULL THEN 1 ELSE 0 END,
               verbosity, command_verbosity, followup_mode, NULL
          FROM overrides_legacy_fast_mode
    """)
    await db.execute("DROP TABLE overrides_legacy_fast_mode")


async def _ensure_thread_delivery_watermarks_thread_scoped(
    db: aiosqlite.Connection,
) -> None:
    rows = await (
        await db.execute("PRAGMA table_info(thread_delivery_watermarks)")
    ).fetchall()
    if any(str(row[1]) == "thread_id" for row in rows):
        return

    await db.execute(
        "ALTER TABLE thread_delivery_watermarks RENAME TO thread_delivery_watermarks_legacy"
    )
    await db.execute("""
        CREATE TABLE thread_delivery_watermarks (
            chat_key TEXT NOT NULL,
            anchor_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            last_message_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(chat_key, anchor_id, thread_id)
        )
    """)
    await db.execute("""
        INSERT INTO thread_delivery_watermarks(
            chat_key, anchor_id, thread_id, last_message_id, updated_at
        )
        SELECT legacy.chat_key,
               legacy.anchor_id,
               COALESCE(message.thread_id, anchor.latest_bridge_id),
               legacy.last_message_id,
               legacy.updated_at
          FROM thread_delivery_watermarks_legacy AS legacy
          LEFT JOIN thread_messages AS message
            ON message.id = legacy.last_message_id
          LEFT JOIN conversation_anchors AS anchor
            ON anchor.chat_key = legacy.chat_key
           AND anchor.anchor_id = legacy.anchor_id
         WHERE COALESCE(message.thread_id, anchor.latest_bridge_id) IS NOT NULL
    """)
    await db.execute("DROP TABLE thread_delivery_watermarks_legacy")


async def _table_exists(db: aiosqlite.Connection, table_name: str) -> bool:
    row = await (
        await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
    ).fetchone()
    return row is not None


def _thread_from_row(row: aiosqlite.Row) -> LogicalThread:
    return LogicalThread(
        thread_id=str(row["thread_id"]),
        chat_key=str(row["chat_key"]),
        title=str(row["title"]),
        codex_thread_id=row["codex_thread_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        turn_count=int(row["turn_count"]),
        awaiting_reply=bool(row["awaiting_reply"]),
        interrupted_notice=bool(row["interrupted_notice"]),
        pending_turn_id=row["pending_turn_id"],
        codex_backend_id=str(row["codex_backend_id"]),
        anchor_id=row["anchor_id"] if "anchor_id" in row.keys() else None,
        expires_at=row["expires_at"] if "expires_at" in row.keys() else None,
        closed_at=row["closed_at"] if "closed_at" in row.keys() else None,
    )


def _bridge_from_row(row: aiosqlite.Row) -> BridgeThread:
    return BridgeThread(
        bridge_id=str(row["bridge_id"]),
        chat_key=str(row["chat_key"]),
        title=str(row["title"]),
        anchor_id=row["anchor_id"],
        codex_thread_id=row["codex_thread_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        turn_count=int(row["turn_count"]),
        awaiting_reply=bool(row["awaiting_reply"]),
        interrupted_notice=bool(row["interrupted_notice"]),
        pending_turn_id=row["pending_turn_id"],
        codex_backend_id=str(row["codex_backend_id"]),
        expires_at=row["expires_at"],
        closed_at=row["closed_at"],
    )


def _anchor_from_row(row: aiosqlite.Row) -> ConversationAnchor:
    return ConversationAnchor(
        anchor_id=str(row["anchor_id"]),
        chat_key=str(row["chat_key"]),
        codex_backend_id=str(row["codex_backend_id"]),
        codex_thread_id=str(row["codex_thread_id"]),
        title=str(row["title"]),
        alias=row["alias"],
        project_id=row["project_id"],
        latest_bridge_id=row["latest_bridge_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        broken_reason=row["broken_reason"],
        archived=bool(row["archived"]),
        latest_bridge_pending_turn_id=(
            row["latest_bridge_pending_turn_id"]
            if "latest_bridge_pending_turn_id" in row.keys()
            else None
        ),
        latest_bridge_awaiting_reply=(
            bool(row["latest_bridge_awaiting_reply"])
            if "latest_bridge_awaiting_reply" in row.keys()
            else False
        ),
        latest_bridge_expires_at=(
            row["latest_bridge_expires_at"]
            if "latest_bridge_expires_at" in row.keys()
            else None
        ),
        latest_bridge_closed_at=(
            row["latest_bridge_closed_at"]
            if "latest_bridge_closed_at" in row.keys()
            else None
        ),
        latest_bridge_pending_approval=(
            bool(row["latest_bridge_pending_approval"])
            if "latest_bridge_pending_approval" in row.keys()
            else False
        ),
        latest_bridge_pending_user_input=(
            bool(row["latest_bridge_pending_user_input"])
            if "latest_bridge_pending_user_input" in row.keys()
            else False
        ),
    )


def _pending_from_row(row: aiosqlite.Row) -> PendingApproval:
    return PendingApproval(
        request_id=int(row["request_id"]),
        chat_key=str(row["chat_key"]),
        logical_thread_id=str(row["logical_thread_id"]),
        codex_thread_id=str(row["codex_thread_id"]),
        codex_backend_id=str(row["codex_backend_id"]),
        turn_id=row["turn_id"],
        method=str(row["method"]),
        command=row["command_text"],
        reason=row["reason"],
        message=row["approval_message"],
        raw_params=json.loads(str(row["raw_params"])),
    )


def _pending_user_input_from_row(row: aiosqlite.Row) -> PendingUserInput:
    return PendingUserInput(
        request_id=int(row["request_id"]),
        chat_key=str(row["chat_key"]),
        logical_thread_id=str(row["logical_thread_id"]),
        codex_thread_id=str(row["codex_thread_id"]),
        codex_backend_id=str(row["codex_backend_id"]),
        turn_id=row["turn_id"],
        method=str(row["method"]),
        questions=_questions_from_json(str(row["questions_json"])),
        selected_answers=_answers_from_json(str(row["selected_answers_json"])),
        awaiting_free_text_question_id=row["awaiting_free_text_question_id"],
        raw_params=json.loads(str(row["raw_params"])),
    )


def _questions_to_json(questions: tuple[UserInputQuestion, ...]) -> str:
    return json.dumps(
        [
            {
                "id": question.question_id,
                "header": question.header,
                "question": question.question,
                "options": [
                    {
                        "label": option.label,
                        "description": option.description,
                    }
                    for option in question.options
                ],
            }
            for question in questions
        ],
        sort_keys=True,
    )


def _questions_from_json(raw: str) -> tuple[UserInputQuestion, ...]:
    data = json.loads(raw)
    if not isinstance(data, list):
        return ()
    questions: list[UserInputQuestion] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        options = item.get("options", [])
        questions.append(
            UserInputQuestion(
                question_id=str(item.get("id", "")),
                header=(
                    str(item["header"]) if isinstance(item.get("header"), str) else None
                ),
                question=str(item.get("question", "")),
                options=tuple(
                    UserInputOption(
                        label=str(option.get("label", "")),
                        description=(
                            str(option.get("description", ""))
                            if isinstance(option, dict)
                            else ""
                        ),
                    )
                    for option in options
                    if isinstance(option, dict)
                ),
            )
        )
    return tuple(questions)


def _answers_to_json(answers: dict[str, tuple[str, ...]]) -> str:
    return json.dumps(
        {question_id: list(values) for question_id, values in answers.items()},
        sort_keys=True,
    )


def _answers_from_json(raw: str) -> dict[str, tuple[str, ...]]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    answers: dict[str, tuple[str, ...]] = {}
    for question_id, values in data.items():
        if not isinstance(values, list):
            continue
        answers[str(question_id)] = tuple(str(value) for value in values)
    return answers


def _callback_token_from_row(row: aiosqlite.Row) -> CallbackToken:
    return CallbackToken(
        token=str(row["token"]),
        chat_key=str(row["chat_key"]),
        topic_id=row["topic_id"],
        action=str(row["action"]),
        payload=json.loads(str(row["payload_json"])),
        expires_at=str(row["expires_at"]),
    )


def _is_expired(expires_at: str) -> bool:
    return datetime.fromisoformat(expires_at) <= datetime.now(UTC)


def _thread_message_from_row(row: aiosqlite.Row) -> ThreadMessage:
    return ThreadMessage(
        message_id=int(row["id"]),
        thread_id=str(row["thread_id"]),
        role=str(row["role"]),
        kind=str(row["kind"]),
        text=str(row["text"]),
        created_at=str(row["created_at"]),
        turn_id=row["turn_id"],
    )


def _final_message_from_row(row: aiosqlite.Row) -> FinalMessageState:
    return FinalMessageState(
        thread_id=str(row["logical_thread_id"]),
        chat_key=str(row["chat_key"]),
        message_id=int(row["message_id"]),
        rendered_text=str(row["rendered_text"]),
    )


def _directory_entry_from_row(row: aiosqlite.Row) -> DirectoryEntry:
    return DirectoryEntry(
        entry_id=int(row["id"]),
        thread_id=str(row["thread_id"]),
        path=str(row["path"]),
        selected_at=str(row["selected_at"]),
    )


def _attachment_job_from_row(row: aiosqlite.Row) -> AttachmentJob:
    return AttachmentJob(
        job_id=int(row["id"]),
        chat_key=str(row["chat_key"]),
        logical_thread_id=str(row["logical_thread_id"]),
        path=str(row["path"]),
        caption=row["caption"],
        status=str(row["status"]),
        error=row["error"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _bridge_control_job_from_row(row: aiosqlite.Row) -> BridgeControlJob:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        payload = {}
    return BridgeControlJob(
        job_id=int(row["id"]),
        chat_key=str(row["chat_key"]),
        logical_thread_id=str(row["logical_thread_id"]),
        kind=str(row["kind"]),
        payload=payload,
        status=str(row["status"]),
        error=row["error"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _project_from_row(row: aiosqlite.Row) -> Project:
    return Project(
        project_id=str(row["project_id"]),
        connection_id=str(row["connection_id"]),
        root_path=str(row["root_path"]),
        label=str(row["label"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _webhook_subscription_from_row(row: aiosqlite.Row) -> WebhookSubscription:
    return WebhookSubscription(
        webhook_id=str(row["webhook_id"]),
        chat_key=str(row["chat_key"]),
        anchor_id=row["anchor_id"] if "anchor_id" in row.keys() else None,
        codex_backend_id=(
            row["codex_backend_id"] if "codex_backend_id" in row.keys() else None
        ),
        codex_thread_id=(
            row["codex_thread_id"] if "codex_thread_id" in row.keys() else None
        ),
        latest_bridge_id=(
            row["latest_bridge_id"] if "latest_bridge_id" in row.keys() else None
        ),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        trigger_count=int(row["trigger_count"]),
        last_triggered_at=row["last_triggered_at"],
    )
