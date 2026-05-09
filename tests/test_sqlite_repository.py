from pathlib import Path
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from codex_telegram.adapters.persistence.sqlite import (
    SQLiteStateRepository,
    SQLiteTelegramProgressStore,
    THREAD_MESSAGE_LIMIT,
)
from codex_telegram.domain import (
    PendingApproval,
    PendingUserInput,
    Project,
    UserInputOption,
    UserInputQuestion,
    SessionOverrides,
)


@pytest.mark.asyncio
async def test_repository_creates_threads_and_overrides(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.ensure_chat("chat:1")
    await repo.create_thread("chat:1", "thread-1", "New thread")

    thread = await repo.get_active_thread("chat:1")
    assert thread is not None
    assert thread.thread_id == "thread-1"

    overrides = SessionOverrides(profile="operator", model="gpt-5.4-mini")
    await repo.upsert_overrides("thread-1", overrides)

    loaded = await repo.get_overrides("thread-1")
    assert loaded.profile == "operator"
    assert loaded.model == "gpt-5.4-mini"

    await repo.upsert_overrides(
        "thread-1",
        SessionOverrides(collaboration_mode="plan"),
    )
    loaded = await repo.get_overrides("thread-1")
    assert loaded.collaboration_mode == "plan"


@pytest.mark.asyncio
async def test_repository_creates_anchor_and_focused_bridge(
    tmp_path: Path,
) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.ensure_chat("chat:1")

    anchor = await repo.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
        title="CI failure",
    )
    duplicate = await repo.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="laptop",
        codex_thread_id="codex-1",
        title="CI failure renamed",
    )
    bridge = await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="CI failure",
        anchor_id=anchor.anchor_id,
        codex_backend_id="laptop",
        expires_at="2026-05-04T00:15:00+00:00",
        focus=True,
    )

    focused = await repo.get_focused_bridge("chat:1")
    anchors = await repo.list_conversation_anchors("chat:1")

    assert duplicate.anchor_id == anchor.anchor_id
    assert bridge.anchor_id == anchor.anchor_id
    assert focused is not None
    assert focused.bridge_id == "bridge-1"
    assert anchors[0].latest_bridge_id == "bridge-1"


@pytest.mark.asyncio
async def test_conversation_anchor_includes_latest_bridge_running_state(
    tmp_path: Path,
) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    anchor = await repo.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Background work",
    )
    bridge = await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="Background work",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
        focus=True,
    )

    await repo.mark_turn_started(bridge.bridge_id, "turn-1")
    anchors = await repo.list_conversation_anchors("chat:1")

    assert anchors[0].latest_bridge_pending_turn_id == "turn-1"
    assert anchors[0].latest_bridge_awaiting_reply is True


@pytest.mark.asyncio
async def test_conversation_anchor_includes_matching_latest_bridge_pending_state(
    tmp_path: Path,
) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    approval_anchor = await repo.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-approval",
        title="Needs approval",
    )
    input_anchor = await repo.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-input",
        title="Needs input",
    )
    unrelated_anchor = await repo.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-unrelated",
        title="Unrelated",
    )
    await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-approval",
        title="Needs approval",
        anchor_id=approval_anchor.anchor_id,
        codex_backend_id="primary",
        focus=False,
    )
    await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-input",
        title="Needs input",
        anchor_id=input_anchor.anchor_id,
        codex_backend_id="primary",
        focus=False,
    )
    await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-unrelated",
        title="Unrelated",
        anchor_id=unrelated_anchor.anchor_id,
        codex_backend_id="primary",
        focus=False,
    )

    await repo.add_pending_request(
        PendingApproval(
            request_id=7,
            chat_key="chat:1",
            logical_thread_id="bridge-approval",
            codex_thread_id="codex-approval",
            codex_backend_id="primary",
            turn_id="turn-approval",
            method="item/commandExecution/requestApproval",
            command="git status",
            reason="request for approval",
            raw_params={"threadId": "codex-approval"},
        )
    )
    await repo.add_pending_user_input(
        PendingUserInput(
            request_id=9,
            chat_key="chat:1",
            logical_thread_id="bridge-input",
            codex_thread_id="codex-input",
            codex_backend_id="primary",
            turn_id="turn-input",
            method="request_user_input",
            questions=(
                UserInputQuestion(
                    question_id="scope",
                    header="Scope",
                    question="Which scope?",
                    options=(
                        UserInputOption(
                            label="Native first",
                            description="Use app-server requests.",
                        ),
                    ),
                ),
            ),
            raw_params={"threadId": "codex-input"},
        )
    )
    await repo.add_pending_request(
        PendingApproval(
            request_id=11,
            chat_key="chat:1",
            logical_thread_id="old-bridge",
            codex_thread_id="codex-unrelated",
            codex_backend_id="primary",
            turn_id="turn-old",
            method="item/commandExecution/requestApproval",
            command="git status",
            reason="request for approval",
            raw_params={"threadId": "codex-unrelated"},
        )
    )

    anchors = {
        anchor.anchor_id: anchor
        for anchor in await repo.list_conversation_anchors("chat:1")
    }

    assert anchors[approval_anchor.anchor_id].latest_bridge_pending_approval is True
    assert anchors[approval_anchor.anchor_id].latest_bridge_pending_user_input is False
    assert anchors[input_anchor.anchor_id].latest_bridge_pending_approval is False
    assert anchors[input_anchor.anchor_id].latest_bridge_pending_user_input is True
    assert anchors[unrelated_anchor.anchor_id].latest_bridge_pending_approval is False
    assert anchors[unrelated_anchor.anchor_id].latest_bridge_pending_user_input is False


@pytest.mark.asyncio
async def test_repository_migrates_legacy_threads_to_anchors_and_bridges(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE chats (
                chat_key TEXT PRIMARY KEY,
                active_thread_id TEXT,
                previous_thread_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE threads (
                thread_id TEXT PRIMARY KEY,
                chat_key TEXT NOT NULL,
                title TEXT NOT NULL,
                codex_thread_id TEXT,
                codex_backend_id TEXT NOT NULL DEFAULT 'primary',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                turn_count INTEGER NOT NULL DEFAULT 0,
                awaiting_reply INTEGER NOT NULL DEFAULT 0,
                interrupted_notice INTEGER NOT NULL DEFAULT 0,
                pending_turn_id TEXT
            );
            CREATE TABLE webhook_subscriptions (
                webhook_id TEXT PRIMARY KEY,
                chat_key TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                name TEXT NOT NULL,
                secret_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                trigger_count INTEGER NOT NULL DEFAULT 0,
                last_triggered_at TEXT
            );
            INSERT INTO chats(chat_key, active_thread_id, previous_thread_id, updated_at)
            VALUES ('chat:1', 'thread-1', NULL, '2026-05-04T00:00:00+00:00');
            INSERT INTO threads(
                thread_id, chat_key, title, codex_thread_id, codex_backend_id,
                created_at, updated_at, turn_count, awaiting_reply,
                interrupted_notice, pending_turn_id
            ) VALUES
                (
                    'thread-1', 'chat:1', 'Bound', 'codex-1', 'laptop',
                    '2026-05-04T00:00:00+00:00', '2026-05-04T00:00:00+00:00',
                    1, 0, 0, NULL
                ),
                (
                    'thread-2', 'chat:1', 'Unbound', NULL, 'laptop',
                    '2026-05-04T00:00:00+00:00', '2026-05-04T00:00:00+00:00',
                    0, 0, 0, NULL
                );
            INSERT INTO webhook_subscriptions(
                webhook_id, chat_key, thread_id, name, secret_hash, enabled,
                created_at, updated_at, trigger_count, last_triggered_at
            ) VALUES
                (
                    'wh_bound', 'chat:1', 'thread-1', 'ci', 'hash', 1,
                    '2026-05-04T00:00:00+00:00', '2026-05-04T00:00:00+00:00',
                    0, NULL
                ),
                (
                    'wh_unbound', 'chat:1', 'thread-2', 'scratch', 'hash', 1,
                    '2026-05-04T00:00:00+00:00', '2026-05-04T00:00:00+00:00',
                    0, NULL
                );
        """)

    repo = SQLiteStateRepository(db_path)
    await repo.initialize()

    focused = await repo.get_focused_bridge("chat:1")
    anchors = await repo.list_conversation_anchors("chat:1")
    subscriptions = await repo.list_webhook_subscriptions(
        chat_key="chat:1",
        include_disabled=True,
    )

    assert focused is not None
    assert focused.bridge_id == "thread-1"
    assert [
        (anchor.codex_backend_id, anchor.codex_thread_id) for anchor in anchors
    ] == [("laptop", "codex-1")]
    by_id = {subscription.webhook_id: subscription for subscription in subscriptions}
    assert by_id["wh_bound"].anchor_id == anchors[0].anchor_id
    assert by_id["wh_bound"].enabled is True
    assert by_id["wh_unbound"].anchor_id is None
    assert by_id["wh_unbound"].enabled is False


@pytest.mark.asyncio
async def test_repository_migrates_legacy_overrides_table(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE overrides (
                thread_id TEXT PRIMARY KEY,
                profile TEXT,
                model TEXT,
                effort TEXT,
                summary TEXT,
                cwd TEXT
            );
            """)

    repo = SQLiteStateRepository(db_path)
    await repo.initialize()
    overrides = SessionOverrides(
        effort="high",
        verbosity="verbose",
        command_verbosity="errors",
        followup_mode="steer",
    )

    await repo.upsert_overrides("thread-1", overrides)
    loaded = await repo.get_overrides("thread-1")

    assert loaded.effort == "high"
    assert loaded.verbosity == "verbose"
    assert loaded.command_verbosity == "errors"
    assert loaded.followup_mode == "steer"
    assert loaded.collaboration_mode is None


@pytest.mark.asyncio
async def test_repository_migrates_legacy_non_nullable_fast_mode(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE overrides (
                thread_id TEXT PRIMARY KEY,
                profile TEXT,
                model TEXT,
                effort TEXT,
                summary TEXT,
                cwd TEXT,
                fast_mode INTEGER NOT NULL DEFAULT 0
            );
            """)

    repo = SQLiteStateRepository(db_path)
    await repo.initialize()

    await repo.upsert_overrides(
        "thread-1",
        SessionOverrides(model="gpt-5.4-mini"),
    )
    loaded = await repo.get_overrides("thread-1")

    assert loaded.model == "gpt-5.4-mini"
    assert loaded.fast_mode is None


@pytest.mark.asyncio
async def test_repository_projects_are_unique_by_connection_and_root(
    tmp_path: Path,
) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()

    home = await repo.upsert_project(
        connection_id="home",
        root_path="/agent/app",
        label="app",
    )
    laptop = await repo.upsert_project(
        connection_id="laptop",
        root_path="/agent/app",
        label="app",
    )
    renamed = await repo.upsert_project(
        connection_id="home",
        root_path="/agent/app",
        label="renamed",
    )

    assert home.project_id == renamed.project_id
    assert laptop.project_id != home.project_id
    assert renamed.label == "renamed"
    assert [
        (project.connection_id, project.root_path, project.label)
        for project in await repo.list_projects()
    ] == [
        ("home", "/agent/app", "renamed"),
        ("laptop", "/agent/app", "app"),
    ]


@pytest.mark.asyncio
async def test_repository_binds_threads_to_projects(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.ensure_chat("chat:1")
    await repo.create_thread("chat:1", "thread-1", "New thread")
    project = await repo.upsert_project(
        connection_id="laptop",
        root_path="/agent/project-a",
        label="project-a",
    )

    await repo.bind_thread_project("thread-1", project.project_id)
    loaded = await repo.get_thread_project("thread-1")
    await repo.clear_thread_project("thread-1")

    assert loaded == project
    assert await repo.get_thread_project("thread-1") is None


@pytest.mark.asyncio
async def test_repository_persists_project_fast_mode_override(
    tmp_path: Path,
) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()

    saved = await repo.upsert_project_overrides(
        "project-1",
        SessionOverrides(model="gpt-5.4-mini", effort="high", fast_mode=False),
    )
    loaded = await repo.get_project_overrides("project-1")

    assert saved.fast_mode is False
    assert loaded.model == "gpt-5.4-mini"
    assert loaded.effort == "high"
    assert loaded.fast_mode is False


@pytest.mark.asyncio
async def test_repository_migrates_workspace_tables_to_primary_connection_projects(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE thread_workspaces (
                thread_id TEXT PRIMARY KEY,
                chat_key TEXT NOT NULL,
                root_path TEXT NOT NULL,
                label TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE workspace_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_key TEXT NOT NULL,
                root_path TEXT NOT NULL,
                label TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO thread_workspaces(
                thread_id, chat_key, root_path, label, updated_at
            ) VALUES (
                'thread-1', 'chat:1', '/agent/project-a', 'old-a', '2026-01-01'
            );
            INSERT INTO workspace_catalog(chat_key, root_path, label, updated_at)
            VALUES ('chat:1', '/agent/project-b', 'old-b', '2026-01-02');
            """)

    repo = SQLiteStateRepository(db_path, default_backend_id="home")
    await repo.initialize()

    projects = await repo.list_projects()
    bound = await repo.get_thread_project("thread-1")
    with sqlite3.connect(db_path) as db:
        old_tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert [(item.connection_id, item.root_path, item.label) for item in projects] == [
        ("home", "/agent/project-b", "old-b"),
        ("home", "/agent/project-a", "old-a"),
    ]
    assert bound == Project(
        project_id=bound.project_id if bound else "",
        connection_id="home",
        root_path="/agent/project-a",
        label="old-a",
        created_at=bound.created_at if bound else "",
        updated_at=bound.updated_at if bound else "",
    )
    assert "thread_workspaces" not in old_tables
    assert "workspace_catalog" not in old_tables


@pytest.mark.asyncio
async def test_repository_marks_waiting_threads_interrupted(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.ensure_chat("chat:1")
    await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="thread-1",
        title="New thread",
        anchor_id=None,
        focus=True,
    )
    await repo.mark_turn_started("thread-1", "turn-1")
    await repo.mark_waiting_threads_interrupted()

    interrupted = await repo.list_interrupted_threads()
    focused = await repo.get_focused_bridge("chat:1")

    assert [thread.thread_id for thread in interrupted] == ["thread-1"]
    assert focused is not None
    assert focused.awaiting_reply is False
    assert focused.pending_turn_id is None
    assert focused.interrupted_notice is True
    assert await repo.take_interrupted_notice("thread-1") is True


@pytest.mark.asyncio
async def test_repository_round_trips_pending_request(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    pending = PendingApproval(
        request_id=7,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="laptop",
        turn_id="turn-1",
        method="item/commandExecution/requestApproval",
        command="git status",
        reason="request for approval",
        message="Guardian reviewed the approach before execution.",
        raw_params={"threadId": "codex-1"},
    )

    await repo.add_pending_request(pending)
    loaded = await repo.get_pending_request("chat:1")
    assert loaded is not None
    assert loaded.command == "git status"
    assert loaded.message == "Guardian reviewed the approach before execution."

    popped = await repo.pop_pending_request(7)
    assert popped is not None
    assert await repo.get_pending_request("chat:1") is None


@pytest.mark.asyncio
async def test_repository_round_trips_pending_user_input(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    pending = PendingUserInput(
        request_id=9,
        chat_key="chat:1",
        logical_thread_id="thread-1",
        codex_thread_id="codex-1",
        codex_backend_id="laptop",
        turn_id="turn-1",
        method="request_user_input",
        questions=(
            UserInputQuestion(
                question_id="scope",
                header="Scope",
                question="Which scope?",
                options=(
                    UserInputOption(
                        label="Native first",
                        description="Use app-server requests.",
                    ),
                    UserInputOption(
                        label="MCP shim",
                        description="Expose a fallback tool.",
                    ),
                ),
            ),
            UserInputQuestion(
                question_id="rollout",
                header="Rollout",
                question="When deploy?",
                options=(UserInputOption(label="Later", description="No deploy."),),
            ),
        ),
        raw_params={"threadId": "codex-1"},
    )

    await repo.add_pending_user_input(pending)
    loaded = await repo.get_pending_user_input("chat:1")
    assert loaded is not None
    assert loaded.codex_backend_id == "laptop"
    assert loaded.questions[0].options[0].label == "Native first"

    await repo.update_pending_user_input_selection(
        9,
        question_id="scope",
        answers=("Native first",),
        awaiting_free_text=False,
    )
    await repo.update_pending_user_input_selection(
        9,
        question_id="rollout",
        answers=(),
        awaiting_free_text=True,
    )
    loaded = await repo.get_pending_user_input("chat:1")
    assert loaded is not None
    assert loaded.selected_answers == {"scope": ("Native first",)}
    assert loaded.awaiting_free_text_question_id == "rollout"

    popped = await repo.pop_pending_user_input(9)
    assert popped is not None
    assert popped.codex_backend_id == "laptop"
    assert popped.selected_answers == {"scope": ("Native first",)}
    assert await repo.get_pending_user_input("chat:1") is None


@pytest.mark.asyncio
async def test_progress_store_round_trips_render_state(tmp_path: Path) -> None:
    store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await store.initialize()

    await store.save_progress("thread-1", message_id=55, rendered_text="Thinking")
    loaded = await store.get_progress("thread-1")

    assert loaded is not None
    assert loaded.message_id == 55
    assert loaded.rendered_text == "Thinking"

    await store.save_progress("thread-1", rendered_text="Done")
    updated = await store.get_progress("thread-1")

    assert updated is not None
    assert updated.message_id == 55
    assert updated.rendered_text == "Done"


async def test_progress_store_round_trips_status_card(tmp_path: Path) -> None:
    store = SQLiteTelegramProgressStore(tmp_path / "state.db")
    await store.initialize()

    await store.save_status_card(
        "chat:1",
        chat_id=1,
        topic_id=None,
        message_id=55,
        rendered_text="Overview:\nFocused",
    )
    loaded = await store.get_status_card("chat:1")

    assert loaded is not None
    assert loaded.chat_key == "chat:1"
    assert loaded.chat_id == 1
    assert loaded.topic_id is None
    assert loaded.message_id == 55
    assert loaded.rendered_text == "Overview:\nFocused"


async def test_repository_updates_new_conversation_title_if_empty(
    tmp_path: Path,
) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    bridge = await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="New conversation",
        anchor_id=None,
        codex_backend_id="primary",
        focus=True,
    )

    await repo.update_thread_title_if_empty(bridge.bridge_id, "Fix overview titles")

    thread = await repo.get_thread(bridge.bridge_id)
    updated_bridge = await repo.get_bridge(bridge.bridge_id)
    assert thread is not None
    assert updated_bridge is not None
    assert thread.title == "Fix overview titles"
    assert updated_bridge.title == "Fix overview titles"


@pytest.mark.asyncio
async def test_repository_round_trips_thread_messages(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.add_thread_message(
        "thread-1",
        role="user",
        kind="prompt",
        text="hello",
    )
    await repo.add_thread_message(
        "thread-1",
        role="assistant",
        kind="final",
        text="world",
        turn_id="turn-1",
    )

    entries = await repo.list_thread_messages("thread-1", limit=10)

    assert [entry.role for entry in entries] == ["user", "assistant"]
    assert entries[1].turn_id == "turn-1"


@pytest.mark.asyncio
async def test_repository_migrates_delivery_watermark_to_message_bridge(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    repo = SQLiteStateRepository(db_path)
    await repo.initialize()
    anchor = await repo.upsert_conversation_anchor(
        chat_key="chat:1",
        codex_backend_id="primary",
        codex_thread_id="codex-1",
        title="Existing Codex thread",
    )
    await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-1",
        title="Existing Codex thread",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
    )
    await repo.add_thread_message(
        "bridge-1",
        role="assistant",
        kind="final",
        text="older bridge answer",
    )
    await repo.create_bridge(
        chat_key="chat:1",
        bridge_id="bridge-2",
        title="Existing Codex thread",
        anchor_id=anchor.anchor_id,
        codex_backend_id="primary",
    )
    await repo.add_thread_message(
        "bridge-2",
        role="assistant",
        kind="final",
        text="newer bridge answer",
    )
    with sqlite3.connect(db_path) as db:
        second_message_id = db.execute(
            "SELECT MAX(id) FROM thread_messages WHERE thread_id = 'bridge-2'"
        ).fetchone()[0]
        db.execute("DROP TABLE thread_delivery_watermarks")
        db.execute("""
            CREATE TABLE thread_delivery_watermarks (
                chat_key TEXT NOT NULL,
                anchor_id TEXT NOT NULL,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(chat_key, anchor_id)
            )
        """)
        db.execute(
            """
            INSERT INTO thread_delivery_watermarks(
                chat_key, anchor_id, last_message_id, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            ("chat:1", anchor.anchor_id, second_message_id, "now"),
        )

    await repo.initialize()

    first_entries = await repo.list_undelivered_final_thread_messages(
        chat_key="chat:1",
        anchor_id=anchor.anchor_id,
        thread_id="bridge-1",
    )
    second_entries = await repo.list_undelivered_final_thread_messages(
        chat_key="chat:1",
        anchor_id=anchor.anchor_id,
        thread_id="bridge-2",
    )

    assert [entry.text for entry in first_entries] == ["older bridge answer"]
    assert second_entries == []


@pytest.mark.asyncio
async def test_repository_lists_undelivered_final_images(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.add_thread_message(
        "bridge-1",
        role="assistant",
        kind="final",
        text="Here is the image.",
        turn_id="turn-1",
    )
    await repo.add_thread_message(
        "bridge-1",
        role="assistant",
        kind="final_image",
        text='{"source": "/agent/generated.png", "caption": "caption"}',
        turn_id="turn-1",
    )

    entries = await repo.list_undelivered_final_thread_messages(
        chat_key="chat:1",
        anchor_id="anchor-1",
        thread_id="bridge-1",
    )

    assert [(entry.kind, entry.text) for entry in entries] == [
        ("final", "Here is the image."),
        ("final_image", '{"source": "/agent/generated.png", "caption": "caption"}'),
    ]


@pytest.mark.asyncio
async def test_repository_trims_thread_messages(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    for index in range(THREAD_MESSAGE_LIMIT + 5):
        await repo.add_thread_message(
            "thread-1",
            role="user",
            kind="prompt",
            text=f"message {index}",
        )

    entries = await repo.list_thread_messages(
        "thread-1",
        limit=THREAD_MESSAGE_LIMIT + 10,
    )

    assert len(entries) == THREAD_MESSAGE_LIMIT
    assert entries[0].text == "message 5"
    assert entries[-1].text == f"message {THREAD_MESSAGE_LIMIT + 4}"


@pytest.mark.asyncio
async def test_repository_lists_recent_directories_newest_first(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()

    await repo.remember_directory("thread-1", "/tmp/one")
    await repo.remember_directory("thread-1", "/tmp/two")
    await repo.remember_directory("thread-1", "/tmp/one")

    entries = await repo.list_directories("thread-1", limit=10)

    assert [entry.path for entry in entries] == ["/tmp/one", "/tmp/two"]


@pytest.mark.asyncio
async def test_repository_queues_and_updates_attachment_jobs(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.ensure_chat("chat:1")
    await repo.create_thread("chat:1", "thread-1", "New thread")

    job = await repo.enqueue_attachment_job(
        "thread-1",
        "/tmp/file.txt",
        caption="caption",
    )
    pending = await repo.list_pending_attachment_jobs(limit=10)

    assert [item.path for item in pending] == ["/tmp/file.txt"]
    assert pending[0].caption == "caption"

    assert job.job_id is not None
    await repo.mark_attachment_job_delivered(job.job_id)
    assert await repo.list_pending_attachment_jobs(limit=10) == []


@pytest.mark.asyncio
async def test_repository_tracks_project_binding_and_catalog(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.ensure_chat("chat:1")
    await repo.create_thread("chat:1", "thread-1", "Thread 1")
    await repo.create_thread("chat:1", "thread-2", "Thread 2")

    first = await repo.upsert_project(
        connection_id="home",
        root_path="/tmp/workspace-a",
        label="workspace-a",
    )
    second = await repo.upsert_project(
        connection_id="home",
        root_path="/tmp/workspace-b",
        label="workspace-b",
    )
    await repo.bind_thread_project("thread-1", first.project_id)
    await repo.bind_thread_project("thread-2", second.project_id)

    loaded = await repo.get_thread_project("thread-1")
    catalog = await repo.list_projects(limit=10)

    assert loaded is not None
    assert loaded.root_path == "/tmp/workspace-a"
    assert [entry.label for entry in catalog] == ["workspace-b", "workspace-a"]


@pytest.mark.asyncio
async def test_repository_tracks_webhook_subscriptions_and_idempotency(
    tmp_path: Path,
) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    await repo.ensure_chat("chat:1")
    await repo.create_thread("chat:1", "thread-1", "New thread")
    await repo.update_codex_thread_binding(
        "thread-1",
        "codex-1",
        codex_backend_id="primary",
    )
    bridge = await repo.get_bridge("thread-1")
    assert bridge is not None

    subscription = await repo.create_webhook_subscription(
        webhook_id="wh_123",
        chat_key="chat:1",
        anchor_id=bridge.anchor_id,
        name="front-door",
        secret_hash="hash-1",
    )

    assert subscription.webhook_id == "wh_123"
    assert subscription.enabled is True
    assert subscription.anchor_id == bridge.anchor_id
    assert await repo.get_webhook_secret_hash("wh_123") == "hash-1"

    listed = await repo.list_webhook_subscriptions(chat_key="chat:1")
    assert [item.name for item in listed] == ["front-door"]

    assert (
        await repo.record_webhook_delivery(
            "wh_123",
            idempotency_key="event-1",
        )
        is True
    )
    assert (
        await repo.record_webhook_delivery(
            "wh_123",
            idempotency_key="event-1",
        )
        is False
    )

    await repo.mark_webhook_triggered("wh_123")
    triggered = await repo.get_webhook_subscription("wh_123")
    assert triggered is not None
    assert triggered.trigger_count == 1
    assert triggered.last_triggered_at is not None

    assert await repo.disable_webhook_subscription("wh_123") is True
    disabled = await repo.get_webhook_subscription("wh_123")
    assert disabled is not None
    assert disabled.enabled is False


@pytest.mark.asyncio
async def test_repository_consumes_callback_tokens_once_with_chat_topic_validation(
    tmp_path: Path,
) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    expires_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()

    token = await repo.create_callback_token(
        chat_key="chat:1:99",
        topic_id=99,
        action="focus_conversation",
        payload={"thread_id": "thread-1"},
        expires_at=expires_at,
    )

    assert (
        await repo.consume_callback_token(
            token,
            chat_key="chat:1",
            topic_id=None,
        )
        is None
    )

    consumed = await repo.consume_callback_token(
        token,
        chat_key="chat:1:99",
        topic_id=99,
    )

    assert consumed is not None
    assert consumed.action == "focus_conversation"
    assert consumed.payload == {"thread_id": "thread-1"}
    assert (
        await repo.consume_callback_token(
            token,
            chat_key="chat:1:99",
            topic_id=99,
        )
        is None
    )


@pytest.mark.asyncio
async def test_repository_rejects_expired_callback_tokens(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db")
    await repo.initialize()
    expires_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()

    token = await repo.create_callback_token(
        chat_key="chat:1",
        topic_id=None,
        action="attach_codex",
        payload={"codex_thread_id": "codex-1"},
        expires_at=expires_at,
    )

    assert (
        await repo.consume_callback_token(
            token,
            chat_key="chat:1",
            topic_id=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_repository_round_trips_codex_backend_id(tmp_path: Path) -> None:
    repo = SQLiteStateRepository(tmp_path / "state.db", default_backend_id="home")
    await repo.initialize()

    await repo.create_thread("chat:1", "thread-1", "Thread")
    await repo.update_codex_thread_binding(
        "thread-1",
        "codex-1",
        codex_backend_id="laptop",
    )

    thread = await repo.get_thread("thread-1")

    assert thread is not None
    assert thread.codex_thread_id == "codex-1"
    assert thread.codex_backend_id == "laptop"
