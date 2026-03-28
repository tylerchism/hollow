"""Smoke tests for INFRA-1: corpus metadata columns on chunks."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from src.memory.schema import ensure_schema
from src.memory.indexer import Indexer
from src.memory.search import Searcher, SearchResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> sqlite3.Connection:
    return ensure_schema(tmp_path / "memory.db")


def _make_indexer(conn: sqlite3.Connection, memory_dir: Path) -> Indexer:
    return Indexer(conn, memory_dir, embedding_client=None)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_chunks_table_has_metadata_columns(tmp_path):
    conn = _make_db(tmp_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    for col in ("person", "platform", "source_type", "source_title", "source_url", "date"):
        assert col in cols, f"Expected column '{col}' in chunks table"


def test_metadata_indexes_exist(tmp_path):
    conn = _make_db(tmp_path)
    indexes = {
        row[1]
        for row in conn.execute(
            "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='chunks'"
        ).fetchall()
    }
    for idx in ("idx_chunks_person", "idx_chunks_source_type", "idx_chunks_date"):
        assert idx in indexes, f"Expected index '{idx}'"


# ---------------------------------------------------------------------------
# Migration test: existing DB without metadata columns gets them added
# ---------------------------------------------------------------------------

def test_migration_adds_columns_to_existing_db(tmp_path):
    """Simulate a legacy DB that has no metadata columns; ensure_schema must add them."""
    db_path = tmp_path / "legacy.db"

    # Build minimal legacy DB without metadata columns
    legacy = sqlite3.connect(str(db_path))
    legacy.execute("PRAGMA journal_mode=WAL")
    legacy.execute(
        "CREATE TABLE chunks ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  file_path TEXT NOT NULL,"
        "  start_line INTEGER NOT NULL,"
        "  end_line INTEGER NOT NULL,"
        "  text TEXT NOT NULL,"
        "  content_hash TEXT NOT NULL,"
        "  embedding BLOB"
        ")"
    )
    legacy.commit()
    legacy.close()

    # Now run ensure_schema — should migrate without error
    conn = ensure_schema(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    for col in ("person", "platform", "source_type", "source_title", "source_url", "date"):
        assert col in cols, f"Migration did not add column '{col}'"


# ---------------------------------------------------------------------------
# Indexer tests
# ---------------------------------------------------------------------------

def test_upsert_file_stores_metadata(tmp_path):
    conn = _make_db(tmp_path)
    memory_dir = tmp_path / "md"
    memory_dir.mkdir()

    md_file = memory_dir / "note.md"
    md_file.write_text("Hello world\n\nSecond paragraph here.")

    indexer = _make_indexer(conn, memory_dir)
    meta = {
        "person": "alice",
        "platform": "discord",
        "source_type": "chat",
        "source_title": "General chat",
        "source_url": "https://example.com/chat/1",
        "date": "2026-03-28",
    }
    asyncio.run(
        indexer.index_file(md_file, force=True, metadata=meta)
    )

    rows = conn.execute(
        "SELECT person, platform, source_type, source_title, source_url, date FROM chunks WHERE file_path = ?",
        (str(md_file),),
    ).fetchall()

    assert len(rows) > 0, "No chunks were inserted"
    for row in rows:
        assert row["person"] == "alice"
        assert row["platform"] == "discord"
        assert row["source_type"] == "chat"
        assert row["source_title"] == "General chat"
        assert row["source_url"] == "https://example.com/chat/1"
        assert row["date"] == "2026-03-28"


def test_upsert_file_null_metadata_by_default(tmp_path):
    conn = _make_db(tmp_path)
    memory_dir = tmp_path / "md"
    memory_dir.mkdir()

    md_file = memory_dir / "bare.md"
    md_file.write_text("Just some text here.")

    indexer = _make_indexer(conn, memory_dir)
    asyncio.run(
        indexer.index_file(md_file, force=True)
    )

    rows = conn.execute(
        "SELECT person, platform, source_type FROM chunks WHERE file_path = ?",
        (str(md_file),),
    ).fetchall()

    assert len(rows) > 0
    for row in rows:
        assert row["person"] is None
        assert row["platform"] is None
        assert row["source_type"] is None


# ---------------------------------------------------------------------------
# SearchResult tests
# ---------------------------------------------------------------------------

def test_search_result_carries_metadata_fields():
    result = SearchResult(
        file_path="/some/file.md",
        start_line=1,
        end_line=5,
        text="hello",
        score=0.9,
        keyword_score=0.8,
        vector_score=0.0,
        person="bob",
        platform="telegram",
        source_type="message",
        source_title="My convo",
        source_url="https://t.me/chat/42",
        date="2026-01-01",
    )
    assert result.person == "bob"
    assert result.platform == "telegram"
    assert result.source_type == "message"
    assert result.source_title == "My convo"
    assert result.source_url == "https://t.me/chat/42"
    assert result.date == "2026-01-01"


def test_search_result_metadata_defaults_to_none():
    result = SearchResult(
        file_path="/some/file.md",
        start_line=1,
        end_line=5,
        text="hello",
        score=0.5,
        keyword_score=0.5,
        vector_score=0.0,
    )
    for field in ("person", "platform", "source_type", "source_title", "source_url", "date"):
        assert getattr(result, field) is None, f"Expected {field} to default to None"


# ---------------------------------------------------------------------------
# Keyword search integration
# ---------------------------------------------------------------------------

def test_keyword_search_returns_metadata(tmp_path):
    conn = _make_db(tmp_path)
    memory_dir = tmp_path / "md"
    memory_dir.mkdir()

    md_file = memory_dir / "kw.md"
    md_file.write_text("The quick brown fox jumps over the lazy dog.")

    indexer = _make_indexer(conn, memory_dir)
    asyncio.run(
        indexer.index_file(
            md_file,
            force=True,
            metadata={"person": "carol", "source_type": "document", "date": "2026-03-01"},
        )
    )

    searcher = Searcher(conn, embedding_client=None)
    results = asyncio.run(
        searcher.search("quick brown fox", limit=5, keyword_weight=1.0, vector_weight=0.0)
    )

    assert len(results) > 0
    top = results[0]
    assert isinstance(top, SearchResult)
    assert top.person == "carol"
    assert top.source_type == "document"
    assert top.date == "2026-03-01"
    assert top.platform is None
