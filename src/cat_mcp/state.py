"""SQLite persistent state management.

Manages projects, grants, workspaces, operations, revisions, and audit log
using a local SQLite database.  Single-process only; connections are not
shared across threads.
"""
# 依据: Task-R-20261126012656439-P0

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from cat_mcp.errors import CatError, ErrorCode

# ---------------------------------------------------------------------------
# Schema version and DDL
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2

SCHEMA_DDL = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    root_identity TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    policy_id TEXT NOT NULL DEFAULT 'default',
    generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE grants (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    scopes TEXT NOT NULL DEFAULT 'read',
    allowed_prefixes TEXT NOT NULL DEFAULT '/',
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_grants_subject_project ON grants(subject, project_id);

CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    cwd TEXT NOT NULL DEFAULT '/',
    cwd_version INTEGER NOT NULL DEFAULT 1,
    project_generation INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_workspaces_subject ON workspaces(subject);

CREATE TABLE operations (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    input_digest TEXT,
    path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared',
    before_version TEXT,
    after_version TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX idx_operations_request ON operations(request_id);
CREATE INDEX idx_operations_status ON operations(status);

CREATE TABLE revisions (
    id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(id),
    before_blob_ref TEXT,
    after_hash TEXT,
    mode TEXT NOT NULL DEFAULT 'text',
    expires_at TEXT
);

CREATE TABLE audit_log (
    event_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    project_id TEXT NOT NULL,
    workspace_id TEXT,
    action TEXT NOT NULL,
    canonical_relative_path TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'success',
    bytes INTEGER DEFAULT 0,
    correlation_id TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_audit_subject ON audit_log(subject);
CREATE INDEX idx_audit_project ON audit_log(project_id);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_id() -> str:
    """Generate an unpredictable URL-safe ID (16 bytes → 22 chars)."""
    # 依据: Task-R-20261126012656439-P0
    return secrets.token_urlsafe(16)


def _now() -> str:
    """Return current UTC time as RFC3339 millisecond string.

    Example: ``2026-11-27T01:55:30.123Z``
    """
    # 依据: Task-R-20261126012656439-P0
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a :class:`sqlite3.Row` to a plain dict, or return *None*."""
    # 依据: Task-R-20261126012656439-P0
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------


class StateManager:
    """SQLite-backed persistent state manager.

    Manages the lifecycle of projects, grants, workspaces, operations,
    revisions, and audit events within a single SQLite database.

    Args:
        db_path: Path to the SQLite database file.  Parent directories are
            created automatically if they do not exist.

    Attributes:
        SCHEMA_VERSION: Current schema version number.
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: str = "/var/lib/cat/state.db") -> None:
        # 依据: Task-R-20261126012656439-P0
        self._db_path = db_path

        # Ensure parent directory exists
        parent = os.path.dirname(db_path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # 物理外键禁用：REFERENCES 仅作文档性声明，引用完整性由应用层保证
        # 依据: Task-R-20261126012656439-P0
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self.run_migrations()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def get_connection(self) -> sqlite3.Connection:
        """Return the underlying SQLite connection.

        The caller must not share this connection across threads.
        """
        # 依据: Task-R-20261126012656439-P0
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        # 依据: Task-R-20261126012656439-P0
        self._conn.close()

    # ------------------------------------------------------------------
    # Schema migrations
    # ------------------------------------------------------------------

    def run_migrations(self) -> None:
        """Apply pending schema migrations.

        Reads ``PRAGMA user_version`` and applies each missing version
        incrementally inside a single transaction.  The initial migration
        (version 0 → 1) creates all six core tables and their indexes.
        """
        # 依据: Task-R-20261126012656439-P0
        cursor = self._conn.execute("PRAGMA user_version")
        current_version: int = cursor.fetchone()[0]

        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer "
                f"than the code supports ({SCHEMA_VERSION})"
            )

        if current_version < 1:
            with self._conn:
                self._conn.executescript(SCHEMA_DDL)
                self._conn.execute("PRAGMA user_version = 1")
                current_version = 1

        if current_version < 2:
            with self._conn:
                self._conn.executescript("""
                    CREATE TABLE upload_sessions (
                        upload_id TEXT PRIMARY KEY,
                        subject TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        path TEXT NOT NULL,
                        expected_size INTEGER NOT NULL,
                        expected_sha256 TEXT NOT NULL,
                        chunk_count INTEGER NOT NULL DEFAULT 0,
                        total_received INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active',
                        started_at TEXT NOT NULL DEFAULT (datetime('now')),
                        expires_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    CREATE INDEX idx_upload_subject ON upload_sessions(subject);
                    CREATE INDEX idx_upload_status ON upload_sessions(status);

                    CREATE TABLE upload_chunks (
                        upload_id TEXT NOT NULL REFERENCES upload_sessions(upload_id),
                        chunk_index INTEGER NOT NULL,
                        chunk_sha256 TEXT NOT NULL,
                        temp_path TEXT NOT NULL,
                        received_at TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (upload_id, chunk_index)
                    );
                """)
                self._conn.execute("PRAGMA user_version = 2")

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def create_project(
        self,
        id: str,
        display_name: str,
        root_path: str,
        root_identity: str = "",
        enabled: bool = True,
        policy_id: str = "default",
        generation: int = 1,
    ) -> dict[str, Any]:
        """Register a new project.

        Unlike other entities, the project *id* is supplied by the
        caller (administrator) rather than auto-generated.

        Returns the inserted row as a dict.
        """
        # 依据: Task-R-20261126012656439-P0
        now = _now()
        self._conn.execute(
            "INSERT INTO projects "
            "(id, display_name, root_path, root_identity, enabled, "
            " policy_id, generation, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                display_name,
                root_path,
                root_identity,
                1 if enabled else 0,
                policy_id,
                generation,
                now,
                now,
            ),
        )
        return self.get_project(id)  # type: ignore[return-value]

    def get_project(self, id: str) -> dict[str, Any] | None:
        """Retrieve a project by *id*, or return *None*."""
        # 依据: Task-R-20261126012656439-P0
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (id,)
        ).fetchone()
        return _row_to_dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        """Return all registered projects."""
        # 依据: Task-R-20261126012656439-P0
        rows = self._conn.execute(
            "SELECT * FROM projects ORDER BY display_name"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_project(
        self,
        id: str,
        display_name: str | None = None,
        root_path: str | None = None,
        root_identity: str | None = None,
        enabled: bool | None = None,
        policy_id: str | None = None,
        generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Update mutable fields of an existing project.

        Only the fields that are not *None* will be changed.  The
        ``updated_at`` timestamp is always refreshed.

        Returns the updated row, or *None* if the project was not found.
        """
        # 依据: Task-R-20261126012656439-P0
        now = _now()
        fields: list[str] = []
        params: list[Any] = []

        if display_name is not None:
            fields.append("display_name = ?")
            params.append(display_name)
        if root_path is not None:
            fields.append("root_path = ?")
            params.append(root_path)
        if root_identity is not None:
            fields.append("root_identity = ?")
            params.append(root_identity)
        if enabled is not None:
            fields.append("enabled = ?")
            params.append(1 if enabled else 0)
        if policy_id is not None:
            fields.append("policy_id = ?")
            params.append(policy_id)
        if generation is not None:
            fields.append("generation = ?")
            params.append(generation)

        if not fields:
            return self.get_project(id)

        fields.append("updated_at = ?")
        params.append(now)
        params.append(id)

        self._conn.execute(
            f"UPDATE projects SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        return self.get_project(id)

    # ------------------------------------------------------------------
    # Grants
    # ------------------------------------------------------------------

    def create_grant(
        self,
        subject: str,
        project_id: str,
        scopes: str = "read",
        allowed_prefixes: str = "/",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Issue a grant to a subject for a project.

        Returns the inserted row as a dict.
        """
        # 依据: Task-R-20261126012656439-P0
        gid = _generate_id()
        now = _now()
        self._conn.execute(
            "INSERT INTO grants "
            "(id, subject, project_id, scopes, allowed_prefixes, "
            " expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (gid, subject, project_id, scopes, allowed_prefixes, expires_at, now),
        )
        return self.get_grants(subject, project_id)[-1]

    def get_grants(
        self, subject: str, project_id: str
    ) -> list[dict[str, Any]]:
        """Return all non-expired grants for a subject + project pair."""
        # 依据: Task-R-20261126012656439-P0
        now = _now()
        rows = self._conn.execute(
            "SELECT * FROM grants "
            "WHERE subject = ? AND project_id = ? "
            "  AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY created_at",
            (subject, project_id, now),
        ).fetchall()
        return [dict(r) for r in rows]

    def revoke_grant(self, id: str) -> bool:
        """Permanently delete a grant by its *id*.

        Returns *True* if a row was deleted.
        """
        # 依据: Task-R-20261126012656439-P0
        cur = self._conn.execute(
            "DELETE FROM grants WHERE id = ?", (id,)
        )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Workspaces
    # ------------------------------------------------------------------

    def create_workspace(
        self,
        subject: str,
        project_id: str,
        cwd: str = "/",
        project_generation: int = 1,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Create a new workspace session.

        Returns the inserted row as a dict.
        """
        # 依据: Task-R-20261126012656439-P0
        wid = _generate_id()
        now = _now()
        self._conn.execute(
            "INSERT INTO workspaces "
            "(id, subject, project_id, cwd, cwd_version, "
            " project_generation, expires_at, status, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, 'active', ?)",
            (wid, subject, project_id, cwd, project_generation, expires_at, now),
        )
        return self.get_workspace(wid)  # type: ignore[return-value]

    def get_workspace(self, id: str) -> dict[str, Any] | None:
        """Retrieve a workspace by *id*, or return *None*."""
        # 依据: Task-R-20261126012656439-P0
        row = self._conn.execute(
            "SELECT * FROM workspaces WHERE id = ?", (id,)
        ).fetchone()
        return _row_to_dict(row)

    def update_workspace_cwd(
        self,
        id: str,
        cwd: str,
        expected_cwd_version: int,
    ) -> dict[str, Any] | None:
        """Update the current working directory of a workspace (CAS).

        The update only succeeds when *expected_cwd_version* matches the
        current value, providing optimistic concurrency control.
        Returns the updated workspace, or *None* on version mismatch.
        """
        # 依据: Task-R-20261126012656439-P0
        cur = self._conn.execute(
            "UPDATE workspaces "
            "SET cwd = ?, cwd_version = cwd_version + 1 "
            "WHERE id = ? AND cwd_version = ? AND status = 'active'",
            (cwd, id, expected_cwd_version),
        )
        if cur.rowcount == 0:
            return None
        return self.get_workspace(id)

    def close_workspace(self, id: str) -> bool:
        """Mark a workspace as closed.

        Returns *True* if a row was updated.
        """
        # 依据: Task-R-20261126012656439-P0
        cur = self._conn.execute(
            "UPDATE workspaces SET status = 'closed' WHERE id = ? AND status = 'active'",
            (id,),
        )
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def create_operation(
        self,
        subject: str,
        workspace_id: str,
        request_id: str,
        path: str,
        input_digest: str | None = None,
    ) -> dict[str, Any]:
        """Create a new write operation in prepared state.

        Returns the inserted row as a dict.
        """
        # 依据: Task-R-20261126012656439-P0
        oid = _generate_id()
        now = _now()
        self._conn.execute(
            "INSERT INTO operations "
            "(id, subject, workspace_id, request_id, input_digest, "
            " path, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?)",
            (oid, subject, workspace_id, request_id, input_digest, path, now),
        )
        return self.get_operation(request_id)  # type: ignore[return-value]

    def update_operation_status(self, id: str, status: str) -> bool:
        """Update the status of an operation.

        Terminal states (``committed``, ``failed``) also set
        ``completed_at``.  Returns *True* if a row was updated.
        """
        # 依据: Task-R-20261126012656439-P0
        if status in ("committed", "failed"):
            now = _now()
            cur = self._conn.execute(
                "UPDATE operations "
                "SET status = ?, completed_at = ? "
                "WHERE id = ?",
                (status, now, id),
            )
        else:
            cur = self._conn.execute(
                "UPDATE operations SET status = ? WHERE id = ?",
                (status, id),
            )
        return cur.rowcount > 0

    def get_operation(self, request_id: str) -> dict[str, Any] | None:
        """Retrieve an operation by *request_id*, or return *None*."""
        # 依据: Task-R-20261126012656439-P0
        row = self._conn.execute(
            "SELECT * FROM operations WHERE request_id = ? ORDER BY started_at DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        return _row_to_dict(row)

    def get_operation_by_id(self, operation_id: str) -> dict[str, Any] | None:
        """Retrieve an operation by its primary key *id*, or return *None*.

        Args:
            operation_id: The ``operations.id`` primary key value.

        Returns:
            The operation row as a dict, or *None* if not found.
        """
        # 依据: CAT-19
        row = self._conn.execute(
            "SELECT * FROM operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
        return _row_to_dict(row)

    def create_operation_idempotent(
        self,
        subject: str,
        workspace_id: str,
        request_id: str,
        path: str,
        input_digest: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create an operation idempotently.

        Checks if an operation with the same *request_id* already exists and
        is in a terminal or committed state (``prepared``, ``committed``,
        ``failed``, or ``in_doubt``).  If one exists:

        * **input_digest matches** — return ``(existing, False)`` (idempotent
          replay, no new row created).
        * **input_digest differs** — raise :class:`CatError` with
          ``REQUEST_ID_CONFLICT`` (same request id, different payload).

        If no eligible operation exists for *request_id*, a new row is
        inserted in ``prepared`` state and this method returns
        ``(new, True)``.

        The SELECT + INSERT is wrapped in an explicit transaction to prevent
        race conditions between the idempotency check and the insert.

        Args:
            subject: 主体标识。
            workspace_id: 工作空间 ID。
            request_id: 请求标识（幂等判断键）。
            path: 操作目标路径。
            input_digest: 输入内容的 SHA-256 哈希。

        Returns:
            ``(operation_dict, is_new)`` 元组。is_new 为 ``True`` 表示新创建。

        Raises:
            CatError: ``REQUEST_ID_CONFLICT`` — 同 request_id 但 input_digest 不同。
        """
        # 依据: CAT-19
        # 使用显式事务防止 SELECT → INSERT 之间的竞态
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.get_operation(request_id)
            if existing is not None and existing.get("status") in (
                "prepared", "committed", "failed", "in_doubt",
            ):
                existing_digest = existing.get("input_digest")
                if existing_digest == input_digest:
                    self._conn.execute("COMMIT")
                    return existing, False
                self._conn.execute("COMMIT")
                raise CatError(
                    ErrorCode.REQUEST_ID_CONFLICT,
                    f"Request ID {request_id!r} already exists with different input",
                )

            oid = _generate_id()
            now = _now()
            self._conn.execute(
                "INSERT INTO operations "
                "(id, subject, workspace_id, request_id, input_digest, "
                " path, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?)",
                (oid, subject, workspace_id, request_id, input_digest, path, now),
            )
            new = _row_to_dict(
                self._conn.execute(
                    "SELECT * FROM operations WHERE id = ?", (oid,)
            ).fetchone()
            )
            self._conn.execute("COMMIT")
        except CatError:
            self._conn.execute("COMMIT")  # COMMIT before re-raising CatError
            raise
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return new, True  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Revisions
    # ------------------------------------------------------------------

    def create_revision(
        self,
        operation_id: str,
        before_blob_ref: str | None = None,
        after_hash: str | None = None,
        mode: str = "text",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Record a version snapshot for an operation.

        Returns the inserted row as a dict.
        """
        # 依据: Task-R-20261126012656439-P0
        rid = _generate_id()
        self._conn.execute(
            "INSERT INTO revisions "
            "(id, operation_id, before_blob_ref, after_hash, mode, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rid, operation_id, before_blob_ref, after_hash, mode, expires_at),
        )
        return dict(
            self._conn.execute(
                "SELECT * FROM revisions WHERE id = ?", (rid,)
            ).fetchone()
        )

    def get_revisions(self, operation_id: str) -> list[dict[str, Any]]:
        """Return all revisions for a given operation."""
        # 依据: Task-R-20261126012656439-P0
        rows = self._conn.execute(
            "SELECT * FROM revisions WHERE operation_id = ? ORDER BY id",
            (operation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def log_audit_event(
        self,
        subject: str,
        project_id: str,
        action: str,
        workspace_id: str | None = None,
        canonical_relative_path: str = "",
        outcome: str = "success",
        bytes: int = 0,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Append an entry to the audit log.

        Returns the inserted row as a dict.
        """
        # 依据: Task-R-20261126012656439-P0
        event_id = _generate_id()
        ts = _now()
        self._conn.execute(
            "INSERT INTO audit_log "
            "(event_id, subject, project_id, workspace_id, action, "
            " canonical_relative_path, outcome, bytes, correlation_id, "
            " timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                subject,
                project_id,
                workspace_id,
                action,
                canonical_relative_path,
                outcome,
                bytes,
                correlation_id,
                ts,
            ),
        )
        return dict(
            self._conn.execute(
                "SELECT * FROM audit_log WHERE event_id = ?", (event_id,)
            ).fetchone()
        )

    def query_audit(
        self,
        subject: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit log entries, optionally filtered by subject or project.

        Results are ordered by timestamp descending.
        """
        # 依据: Task-R-20261126012656439-P0
        conditions: list[str] = []
        params: list[Any] = []

        if subject is not None:
            conditions.append("subject = ?")
            params.append(subject)
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        rows = self._conn.execute(
            f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Upload sessions (CAT-33)
    # ------------------------------------------------------------------

    def create_upload_session(
        self,
        upload_id: str,
        subject: str,
        workspace_id: str,
        project_id: str,
        path: str,
        expected_size: int,
        expected_sha256: str,
        expires_at: str,
    ) -> dict[str, Any]:
        """Create a new chunked upload session.

        Returns the inserted row as a dict.
        """
        now = _now()
        self._conn.execute(
            "INSERT INTO upload_sessions "
            "(upload_id, subject, workspace_id, project_id, path, "
            " expected_size, expected_sha256, chunk_count, total_received, "
            " status, started_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'active', ?, ?)",
            (
                upload_id, subject, workspace_id, project_id, path,
                expected_size, expected_sha256, now, expires_at,
            ),
        )
        row = self._conn.execute(
            "SELECT * FROM upload_sessions WHERE upload_id = ?", (upload_id,)
        ).fetchone()
        return dict(row)

    def get_upload_session(self, upload_id: str) -> dict[str, Any] | None:
        """Retrieve an upload session by *upload_id*, or return *None*."""
        row = self._conn.execute(
            "SELECT * FROM upload_sessions WHERE upload_id = ?", (upload_id,)
        ).fetchone()
        return _row_to_dict(row)

    def update_upload_session_status(
        self, upload_id: str, status: str,
    ) -> bool:
        """Update the status of an upload session.

        Terminal states also set ``completed_at``.
        Returns *True* if a row was updated.
        """
        if status in ("completed", "aborted", "expired"):
            now = _now()
            cur = self._conn.execute(
                "UPDATE upload_sessions "
                "SET status = ?, completed_at = ? "
                "WHERE upload_id = ?",
                (status, now, upload_id),
            )
        else:
            cur = self._conn.execute(
                "UPDATE upload_sessions SET status = ? WHERE upload_id = ?",
                (status, upload_id),
            )
        return cur.rowcount > 0

    def increment_upload_chunks(
        self, upload_id: str, size_bytes: int,
    ) -> bool:
        """Increment chunk count and received size for an upload session."""
        cur = self._conn.execute(
            "UPDATE upload_sessions "
            "SET chunk_count = chunk_count + 1, "
            "    total_received = total_received + ? "
            "WHERE upload_id = ? AND status = 'active'",
            (size_bytes, upload_id),
        )
        return cur.rowcount > 0

    def record_upload_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        chunk_sha256: str,
        temp_path: str,
    ) -> dict[str, Any]:
        """Record a received chunk.

        Returns the inserted row as a dict.
        """
        now = _now()
        self._conn.execute(
            "INSERT OR REPLACE INTO upload_chunks "
            "(upload_id, chunk_index, chunk_sha256, temp_path, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (upload_id, chunk_index, chunk_sha256, temp_path, now),
        )
        row = self._conn.execute(
            "SELECT * FROM upload_chunks "
            "WHERE upload_id = ? AND chunk_index = ?",
            (upload_id, chunk_index),
        ).fetchone()
        return dict(row)

    def get_upload_chunks(self, upload_id: str) -> list[dict[str, Any]]:
        """Return all chunks for an upload session, ordered by index."""
        rows = self._conn.execute(
            "SELECT * FROM upload_chunks "
            "WHERE upload_id = ? ORDER BY chunk_index",
            (upload_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_upload_session(self, upload_id: str) -> bool:
        """Delete an upload session and its chunks."""
        self._conn.execute(
            "DELETE FROM upload_chunks WHERE upload_id = ?", (upload_id,)
        )
        cur = self._conn.execute(
            "DELETE FROM upload_sessions WHERE upload_id = ?", (upload_id,)
        )
        return cur.rowcount > 0

    def expire_stale_uploads(
        self, before: str,
    ) -> list[dict[str, Any]]:
        """Return all active uploads that have expired."""
        rows = self._conn.execute(
            "SELECT * FROM upload_sessions "
            "WHERE status = 'active' AND expires_at < ?",
            (before,),
        ).fetchall()
        return [dict(r) for r in rows]
