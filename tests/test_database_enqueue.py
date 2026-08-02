"""Regression tests for the SQLite job queue.

The enqueue path must honour its documented lazy-project contract: a
``project_id`` provided to ``enqueue_job`` is created in ``projects``
before the job INSERT, so the ``jobs.project_id`` foreign key never
fails. The dream/soul fast-path enqueues with ``project_id="soul"`` and
does not create the project itself (routes.py), so this laziness is the
only thing standing between a dream request and a 500.

The tests run against the *real* ``Database`` class on a temp file. The
harness conftest replaces ``database.Database`` with ``_NoOpDatabase``
for app-level tests, so the real class is loaded from an isolated module
copy here.
"""

import importlib.util
from pathlib import Path

import pytest
import pytest_asyncio

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "real_database", _PROJECT_ROOT / "database.py"
)
_real_database = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_real_database)
Database = _real_database.Database


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test_queue.db")
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_enqueue_job_creates_project_lazily(db: Database) -> None:
    """enqueue_job with project_id creates the project row (FK satisfied)."""
    job_id = await db.enqueue_job(
        messages_json='[{"role": "user", "content": "dream"}]',
        priority=3,
        intent="ARCHITECT",
        project_id="soul",
        lane="lane_a",
    )

    job = await db.get_job(job_id)
    assert job is not None
    assert job["project_id"] == "soul"

    projects = await db.get_all_projects()
    assert any(p["id"] == "soul" for p in projects)


@pytest.mark.asyncio
async def test_enqueue_job_without_project_id_still_works(db: Database) -> None:
    """A None project_id is accepted and the job has no project FK."""
    job_id = await db.enqueue_job(
        messages_json="[]",
        priority=2,
        intent="CHAT",
        project_id=None,
    )
    job = await db.get_job(job_id)
    assert job is not None
    assert job["project_id"] is None


@pytest.mark.asyncio
async def test_enqueue_job_slugs_display_name(db: Database) -> None:
    """A display name is slugified before the FK insert."""
    job_id = await db.enqueue_job(
        messages_json="[]",
        priority=1,
        intent="CODE",
        project_id="My Project",
    )
    job = await db.get_job(job_id)
    assert job is not None
    assert job["project_id"] == "my-project"
