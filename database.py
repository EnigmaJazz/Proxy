"""
database.py - SQLite WAL-mode queue & state manager for Kinver Hub.

Replaces the volatile RAM asyncio.PriorityQueue + JSON-file persistence
with a durable aiosqlite database backed by WAL journaling.  Integrates
with the sqlite-vec extension for semantic search (tool-loop detection,
semantic cache, lessons-learned lookups).

Schema
------
- **jobs**: Primary queue table with priority preemption tiers,
  partial-stream tracking, and multi-attempt failure counters.
- **projects**: Persistent project awareness for cache segmentation.
- **semantic_cache**: Factual-query cache powered by sqlite-vec.
- **lessons_learned**: Project-specific validated patterns for
  knowledge-cutoff bypass.
- **stream_chunks**: Granular partial-generation checkpointing.

All public methods are ``async`` and use an aiosqlite connection pool
behind a simple single-connection wrapper (the sqlite-vec extension
requires loading per-connection).

Usage::

    db = Database(DB_PATH)
    await db.initialize()
    job_id = await db.enqueue_job(...)

Maintainers: James Stansfield
"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional, Any

import aiosqlite

from constants import DB_PATH, MIGRATIONS, get_logger

logger = get_logger("proxy.database")

# ---------------------------------------------------------------------------
# Schema version constant — bump when migrations change
# ---------------------------------------------------------------------------
SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Database manager class
# ---------------------------------------------------------------------------


class Database:
    """
    Async interface to the ai_queue.db SQLite database.

    Wraps an ``aiosqlite.Connection`` opened in WAL mode.  All writes are
    serialized through ``_execute_write()`` which acquires an internal
    ``asyncio.Lock`` — sqlite-vec extension loading requires a single
    writer, and this keeps contention predictable under uvloop.

    Parameters
    ----------
    db_path : Path
        Filesystem path to the SQLite database file.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()
        self._vec_loaded: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """
        Open the database connection, enable WAL, run schema migrations,
        and attempt to load the sqlite-vec extension.
        """
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row

        # Enable WAL + perf pragmas (safe outside transaction)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")

        # Run schema migrations
        for stmt in MIGRATIONS:
            if stmt.strip().upper().startswith("PRAGMA"):
                continue  # already applied above
            await self._conn.execute(stmt)

        await self._conn.commit()

        # Attempt to load sqlite-vec extension (non-fatal if unavailable)
        await self._try_load_vec()

        logger.info(
            "Database initialised at %s (WAL mode, vec=%s)",
            self._db_path,
            self._vec_loaded,
        )

    async def close(self) -> None:
        """Close the database connection cleanly."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed")

    # ------------------------------------------------------------------
    # sqlite-vec extension loading
    # ------------------------------------------------------------------

    async def _try_load_vec(self) -> None:
        """
        Attempt to load the sqlite-vec extension.  Non-fatal — semantic
        features will degrade gracefully if the extension is unavailable.
        """
        try:
            await self._conn.execute("SELECT load_extension('sqlite-vec')")
            self._vec_loaded = True
            logger.info("sqlite-vec extension loaded successfully")
        except (sqlite3.Error, OSError) as exc:
            logger.warning(
                "sqlite-vec extension not available (%s) — "
                "semantic features will be disabled",
                exc,
            )
            self._vec_loaded = False

    @property
    def vec_available(self) -> bool:
        """Return True if sqlite-vec was loaded successfully."""
        return self._vec_loaded

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_write(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        """
        Execute a write statement under the internal write lock to
        serialise concurrent mutations.
        """
        async with self._write_lock:
            cursor = await self._conn.execute(sql, params)
            await self._conn.commit()
            return cursor

    @staticmethod
    def _now_iso() -> str:
        """Return current UTC timestamp as ISO-8601 string."""
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ------------------------------------------------------------------
    # Job queue operations
    # ------------------------------------------------------------------

    async def enqueue_job(
        self,
        messages_json: str,
        priority: int = 2,
        intent: str = "CHAT",
        project_id: Optional[str] = None,
        tools_json: Optional[str] = None,
        parameters_json: Optional[str] = None,
        lane: str = "lane_a",
        is_lane_b: bool = False,
        caller_type: str = "AGENTIC",
        model_override: Optional[str] = None,
    ) -> str:
        """
        Insert a new job into the queue and return its UUID.

        Parameters
        ----------
        messages_json : str
            JSON-serialised message array.
        priority : int
            1=HIGH, 2=NORMAL, 3=BACKGROUND, 4=DAEMON.
        intent : str
            One of CHAT/CODE/TOOL/SCHOLAR/PROFESSIONAL/CREATIVE/ARCHITECT.
        project_id : str or None
            Persistent project slug; created lazily if provided.
        tools_json : str or None
            JSON-serialised tool definitions.
        parameters_json : str or None
            JSON-serialised generation params (temperature, top_p, etc.).
        lane : str
            ``"lane_a"`` or ``"lane_b"``.
        is_lane_b : bool
            True if routed via IDE passthrough.
        caller_type : str
            ``"IDE"`` or ``"AGENTIC"``.
        model_override : str or None
            Explicit model name if caller requested a specific service.

        Returns
        -------
        str
            The new job's UUID.
        """
        job_id = str(uuid.uuid4())
        now = self._now_iso()

        # Lazy project creation: the documented contract says the project
        # slug is created when provided, and the ``jobs.project_id`` FK
        # requires the row to exist before the INSERT (the dream path
        # enqueues with ``project_id="soul"`` without creating it first).
        if project_id:
            project_id = await self.get_or_create_project(project_id)

        await self._execute_write(
            """INSERT INTO jobs (
                id, priority, state, intent, project_id,
                messages_json, tools_json, parameters_json,
                lane, is_lane_b, caller_type, model_override,
                created_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, priority, intent, project_id,
                messages_json, tools_json, parameters_json,
                lane, int(is_lane_b), caller_type, model_override,
                now,
            ),
        )

        logger.debug("Job %s enqueued (priority=%d, intent=%s, lane=%s)",
                      job_id, priority, intent, lane)
        return job_id

    async def dequeue_next(self, max_priority: int = 2) -> Optional[dict[str, Any]]:
        """
        Atomically claim the next queued job whose priority <= *max_priority*.

        Returns the job as a dict, or ``None`` if no eligible job exists.
        The returned job is marked ``'active'`` in the database.
        """
        async with self._write_lock:
            cursor = await self._conn.execute(
                """SELECT * FROM jobs
                   WHERE state = 'queued' AND priority <= ?
                   ORDER BY priority ASC, created_at ASC
                   LIMIT 1""",
                (max_priority,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None

            job = dict(row)
            now = self._now_iso()
            await self._conn.execute(
                "UPDATE jobs SET state = 'active', started_at = ? WHERE id = ?",
                (now, job["id"]),
            )
            await self._conn.commit()
            logger.debug("Job %s dequeued (priority=%d)", job["id"], job["priority"])
            return job

    async def update_partial_content(self, job_id: str, text: str) -> None:
        """
        Overwrite the accumulated streaming text for *job_id*.
        Used for crash recovery — the last written value is the
        recovery checkpoint.
        """
        await self._execute_write(
            "UPDATE jobs SET partial_content = ? WHERE id = ?",
            (text, job_id),
        )

    async def record_stream_chunk(
        self, job_id: str, seq: int, chunk_json: str
    ) -> None:
        """Persist a single SSE chunk for granular stream recovery."""
        await self._execute_write(
            """INSERT INTO stream_chunks (job_id, seq, chunk_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (job_id, seq, chunk_json, self._now_iso()),
        )

    async def complete_job(
        self, job_id: str, finish_reason: str, full_content: str = ""
    ) -> None:
        """Mark a job as successfully completed."""
        await self._execute_write(
            """UPDATE jobs
               SET state = 'completed',
                   finish_reason = ?,
                   partial_content = ?,
                   completed_at = ?,
                   failure_count = 0
               WHERE id = ?""",
            (finish_reason, full_content, self._now_iso(), job_id),
        )
        logger.info("Job %s completed (finish_reason=%s)", job_id, finish_reason)

    async def fail_job(self, job_id: str) -> int:
        """
        Increment the failure counter for *job_id* and set state to 'failed'.

        Returns the new failure count.
        """
        async with self._write_lock:
            cursor = await self._conn.execute(
                "SELECT failure_count FROM jobs WHERE id = ?", (job_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                return 0
            new_count = row["failure_count"] + 1
            await self._conn.execute(
                "UPDATE jobs SET failure_count = ?, state = 'failed' WHERE id = ?",
                (new_count, job_id),
            )
            await self._conn.commit()
            logger.warning("Job %s failed (failure_count=%d)", job_id, new_count)
            return new_count

    async def escalate_job(self, job_id: str, new_tier: str) -> None:
        """
        Update the job's current model tier and reset state to 'queued'
        for re-processing at a higher tier.
        """
        await self._execute_write(
            """UPDATE jobs
               SET current_tier = ?, state = 'queued'
               WHERE id = ?""",
            (new_tier, job_id),
        )
        logger.info("Job %s escalated to tier %s", job_id, new_tier)

    async def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single job by ID."""
        cursor = await self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_pending_jobs(self) -> list[dict[str, Any]]:
        """
        Return all jobs in 'queued' or 'active' state.
        Used at startup to rebuild the in-memory working set.
        """
        cursor = await self._conn.execute(
            "SELECT * FROM jobs WHERE state IN ('queued', 'active') ORDER BY priority ASC, created_at ASC"
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ------------------------------------------------------------------
    # Project operations
    # ------------------------------------------------------------------

    async def get_or_create_project(
        self, name: str, root_path: str = ""
    ) -> str:
        """
        Return the project ID for *name*, creating it if it doesn't exist.

        The project ID is a slugified version of *name* (lowercase, spaces
        replaced with hyphens).
        """
        project_id = name.lower().strip().replace(" ", "-")
        if not project_id:
            project_id = "general"

        now = self._now_iso()

        cursor = await self._conn.execute(
            "SELECT id FROM projects WHERE id = ?", (project_id,)
        )
        existing = await cursor.fetchone()
        if existing:
            # Touch last_active_at
            await self._execute_write(
                "UPDATE projects SET last_active_at = ? WHERE id = ?",
                (now, project_id),
            )
            return project_id

        # Create new project
        await self._execute_write(
            """INSERT INTO projects (id, display_name, root_path, created_at, last_active_at)
               VALUES (?, ?, ?, ?, ?)""",
            (project_id, name, root_path, now, now),
        )
        logger.info("Project created: %s", project_id)
        return project_id

    # ------------------------------------------------------------------
    # Semantic cache operations
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_query(query_text: str) -> str:
        """Return a SHA-256 hex digest of the normalised query text."""
        normalised = query_text.strip().lower()
        return hashlib.sha256(normalised.encode()).hexdigest()

    async def cache_lookup(self, query_text: str) -> Optional[str]:
        """
        Check if *query_text* has a cached response that hasn't expired.
        Returns the cached response text or None.
        """
        query_hash = self._hash_query(query_text)
        cursor = await self._conn.execute(
            """SELECT response_text, expires_at FROM semantic_cache
               WHERE query_hash = ?""",
            (query_hash,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row["expires_at"] < self._now_iso():
            # Expired — clean up
            await self._execute_write(
                "DELETE FROM semantic_cache WHERE query_hash = ?", (query_hash,)
            )
            return None
        # Increment hit counter
        await self._execute_write(
            "UPDATE semantic_cache SET hit_count = hit_count + 1 WHERE query_hash = ?",
            (query_hash,),
        )
        logger.debug("Cache hit for query hash %s", query_hash[:12])
        return row["response_text"]

    async def cache_store(
        self,
        query_text: str,
        response_text: str,
        ttl_seconds: int = 3600,
        embedding: Optional[bytes] = None,
    ) -> None:
        """
        Store a query/response pair in the semantic cache with a TTL.

        Parameters
        ----------
        query_text : str
            The user query.
        response_text : str
            The model's response.
        ttl_seconds : int
            Time-to-live in seconds (default 1 hour).
        embedding : bytes or None
            Optional sqlite-vec embedding blob for the query.
        """
        query_hash = self._hash_query(query_text)
        now = self._now_iso()
        # Calculate expiry from now + ttl
        expires_at = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() + ttl_seconds),
        )
        await self._execute_write(
            """INSERT OR REPLACE INTO semantic_cache
               (query_hash, query_text, response_text, embedding, hit_count, created_at, expires_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (query_hash, query_text[:500], response_text, embedding, now, expires_at),
        )
        logger.debug("Cache stored for query hash %s", query_hash[:12])

    # ------------------------------------------------------------------
    # Lessons-learned operations (knowledge-cutoff bypass)
    # ------------------------------------------------------------------

    async def check_lessons_learned(
        self, project_id: str, flagged_text: str
    ) -> bool:
        """
        Search the lessons_learned table for patterns that validate
        *flagged_text* within *project_id*.

        Returns True if a matching lesson is found, False otherwise.

        NOTE: Full semantic search requires sqlite-vec.  If the extension
        is unavailable this falls back to a simple substring match.
        """
        if not self._vec_loaded:
            # Fallback: substring match against pattern_text
            cursor = await self._conn.execute(
                """SELECT pattern_text FROM lessons_learned
                   WHERE project_id = ?""",
                (project_id,),
            )
            rows = await cursor.fetchall()
            for row in rows:
                if row["pattern_text"].lower() in flagged_text.lower():
                    logger.info("Lessons-learned substring match for job in project %s", project_id)
                    return True
            return False

        # sqlite-vec semantic search path (implementation will follow
        # once the vec0 virtual table is created during a future migration)
        logger.debug("sqlite-vec semantic search not yet implemented — returning False")
        return False

    async def store_lesson(
        self,
        project_id: str,
        pattern_text: str,
        source: str = "",
        embedding: Optional[bytes] = None,
    ) -> int:
        """
        Store a validated pattern in the lessons_learned table.

        Returns the new row ID.
        """
        async with self._write_lock:
            cursor = await self._conn.execute(
                """INSERT INTO lessons_learned
                   (project_id, pattern_text, embedding, source, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (project_id, pattern_text, embedding, source, self._now_iso()),
            )
            await self._conn.commit()
            return cursor.lastrowid

    # ------------------------------------------------------------------
    # Stream chunk cleanup (called after job completion)
    # ------------------------------------------------------------------

    async def purge_stream_chunks(self, job_id: str) -> None:
        """Delete all stream_chunk rows for a completed job."""
        await self._execute_write(
            "DELETE FROM stream_chunks WHERE job_id = ?", (job_id,)
        )

    async def get_all_projects(self) -> list[dict[str, Any]]:
        """
        Return all projects ordered by most recently active first.

        Used by the frontdesk classifier to inject the project roster
        into the classification prompt.
        """
        cursor = await self._conn.execute(
            "SELECT * FROM projects ORDER BY last_active_at DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]
