"""Tests for KA-1: knowledge graph schema and helper functions."""

import sqlite3
import tempfile
import os
from pathlib import Path

import pytest

from src.memory.knowledge_graph import (
    ensure_graph_schema,
    upsert_entity,
    add_relationship,
    add_source,
    add_alias,
    find_entity,
    get_related,
    search_entities,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def graph_conn(tmp_path):
    """Return an open in-memory-equivalent graph DB connection."""
    db_path = tmp_path / "test-graph.db"
    conn = ensure_graph_schema(db_path)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

def test_all_tables_exist(graph_conn):
    tables = {
        row[0]
        for row in graph_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "entities" in tables
    assert "relationships" in tables
    assert "entity_sources" in tables
    assert "entity_aliases" in tables


def test_wal_mode_enabled(graph_conn):
    mode = graph_conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_foreign_keys_enabled(graph_conn):
    fk = graph_conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_required_indexes_exist(graph_conn):
    indexes = {
        row[1]
        for row in graph_conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_entities_name_type" in indexes
    assert "idx_rel_from" in indexes
    assert "idx_rel_to" in indexes
    assert "idx_entity_sources_entity" in indexes
    assert "idx_alias" in indexes


def test_fts5_virtual_table_exists(graph_conn):
    tables = {
        row[0]
        for row in graph_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "entities_fts" in tables


def test_ensure_graph_schema_idempotent(tmp_path):
    """Calling ensure_graph_schema twice should not raise or create duplicates."""
    db_path = tmp_path / "idempotent.db"
    conn1 = ensure_graph_schema(db_path)
    conn1.close()
    conn2 = ensure_graph_schema(db_path)
    tables = {
        row[0]
        for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "entities" in tables
    conn2.close()


# ---------------------------------------------------------------------------
# upsert_entity tests
# ---------------------------------------------------------------------------

def test_upsert_entity_creates_new(graph_conn):
    eid = upsert_entity(graph_conn, "magnesium", "substance", "A mineral element")
    assert isinstance(eid, int)
    assert eid > 0


def test_upsert_entity_idempotent(graph_conn):
    """Calling twice with the same name+type must not create duplicates."""
    eid1 = upsert_entity(graph_conn, "magnesium", "substance", "A mineral element")
    eid2 = upsert_entity(graph_conn, "magnesium", "substance", "Updated description")
    assert eid1 == eid2
    count = graph_conn.execute(
        "SELECT COUNT(*) FROM entities WHERE name='magnesium' AND type='substance'"
    ).fetchone()[0]
    assert count == 1


def test_upsert_entity_updates_description(graph_conn):
    eid = upsert_entity(graph_conn, "magnesium", "substance", "Original description")
    upsert_entity(graph_conn, "magnesium", "substance", "Updated description")
    row = graph_conn.execute(
        "SELECT description FROM entities WHERE id=?", (eid,)
    ).fetchone()
    assert row[0] == "Updated description"


def test_upsert_entity_different_types_are_distinct(graph_conn):
    eid1 = upsert_entity(graph_conn, "calcium", "substance")
    eid2 = upsert_entity(graph_conn, "calcium", "concept")
    assert eid1 != eid2


# ---------------------------------------------------------------------------
# add_relationship tests
# ---------------------------------------------------------------------------

def test_add_relationship(graph_conn):
    eid1 = upsert_entity(graph_conn, "magnesium", "substance")
    eid2 = upsert_entity(graph_conn, "daniel-vitalis", "person")
    rid = add_relationship(graph_conn, eid2, eid1, "discusses", context="DV talks about mag")
    assert isinstance(rid, int)
    assert rid > 0


def test_add_relationship_stored_correctly(graph_conn):
    eid1 = upsert_entity(graph_conn, "ATP", "substance")
    eid2 = upsert_entity(graph_conn, "mitochondria", "concept")
    rid = add_relationship(graph_conn, eid2, eid1, "produces", context="mito produces ATP", weight=0.9)
    row = graph_conn.execute(
        "SELECT from_entity, to_entity, label, context, weight FROM relationships WHERE id=?",
        (rid,),
    ).fetchone()
    assert row["from_entity"] == eid2
    assert row["to_entity"] == eid1
    assert row["label"] == "produces"
    assert row["context"] == "mito produces ATP"
    assert abs(row["weight"] - 0.9) < 0.001


# ---------------------------------------------------------------------------
# add_source tests
# ---------------------------------------------------------------------------

def test_add_source(graph_conn):
    eid = upsert_entity(graph_conn, "cold thermogenesis", "protocol")
    add_source(graph_conn, eid, "corpus_chunk", "abc123", source_db="/data/corpus/dv/memory.db")
    row = graph_conn.execute(
        "SELECT source_type, source_id, source_db FROM entity_sources WHERE entity_id=?", (eid,)
    ).fetchone()
    assert row["source_type"] == "corpus_chunk"
    assert row["source_id"] == "abc123"
    assert row["source_db"] == "/data/corpus/dv/memory.db"


def test_add_source_deduplicates(graph_conn):
    eid = upsert_entity(graph_conn, "cold thermogenesis", "protocol")
    add_source(graph_conn, eid, "corpus_chunk", "abc123")
    add_source(graph_conn, eid, "corpus_chunk", "abc123")  # duplicate
    count = graph_conn.execute(
        "SELECT COUNT(*) FROM entity_sources WHERE entity_id=?", (eid,)
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# add_alias tests
# ---------------------------------------------------------------------------

def test_add_alias(graph_conn):
    eid = upsert_entity(graph_conn, "magnesium", "substance")
    add_alias(graph_conn, eid, "mag")
    row = graph_conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias='mag'"
    ).fetchone()
    assert row["entity_id"] == eid


def test_add_alias_duplicate_is_silent(graph_conn):
    eid = upsert_entity(graph_conn, "magnesium", "substance")
    add_alias(graph_conn, eid, "mag")
    add_alias(graph_conn, eid, "mag")  # should not raise
    count = graph_conn.execute(
        "SELECT COUNT(*) FROM entity_aliases WHERE alias='mag'"
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# find_entity tests
# ---------------------------------------------------------------------------

def test_find_entity_by_canonical_name(graph_conn):
    upsert_entity(graph_conn, "magnesium", "substance", "A mineral")
    result = find_entity(graph_conn, "magnesium")
    assert result is not None
    assert result["name"] == "magnesium"
    assert result["type"] == "substance"


def test_find_entity_by_alias(graph_conn):
    eid = upsert_entity(graph_conn, "magnesium", "substance")
    add_alias(graph_conn, eid, "mag")
    result = find_entity(graph_conn, "mag")
    assert result is not None
    assert result["name"] == "magnesium"


def test_find_entity_case_insensitive(graph_conn):
    upsert_entity(graph_conn, "magnesium", "substance")
    result = find_entity(graph_conn, "MAGNESIUM")
    assert result is not None


def test_find_entity_returns_none_for_unknown(graph_conn):
    result = find_entity(graph_conn, "nonexistent_entity_xyz")
    assert result is None


# ---------------------------------------------------------------------------
# get_related tests
# ---------------------------------------------------------------------------

def _setup_graph(conn):
    """Create a small test graph: dv --discusses--> magnesium --relates_to--> calcium"""
    eid_dv = upsert_entity(conn, "daniel-vitalis", "person")
    eid_mag = upsert_entity(conn, "magnesium", "substance")
    eid_cal = upsert_entity(conn, "calcium", "substance")
    eid_cm = upsert_entity(conn, "chris-masterjohn", "person")
    add_relationship(conn, eid_dv, eid_mag, "discusses")
    add_relationship(conn, eid_mag, eid_cal, "relates_to")
    add_relationship(conn, eid_cm, eid_mag, "discusses")
    return eid_dv, eid_mag, eid_cal, eid_cm


def test_get_related_depth_1(graph_conn):
    eid_dv, eid_mag, eid_cal, eid_cm = _setup_graph(graph_conn)
    related = get_related(graph_conn, eid_mag, depth=1)
    names = {r["name"] for r in related}
    assert "daniel-vitalis" in names
    assert "chris-masterjohn" in names
    assert "calcium" in names
    # Should not include magnesium itself
    assert "magnesium" not in names


def test_get_related_depth_2(graph_conn):
    eid_dv, eid_mag, eid_cal, eid_cm = _setup_graph(graph_conn)
    # Starting from dv, depth=2 should reach calcium (dv->mag->calcium)
    related = get_related(graph_conn, eid_dv, depth=2)
    names = {r["name"] for r in related}
    assert "magnesium" in names
    assert "calcium" in names


def test_get_related_depth_check(graph_conn):
    eid_dv, eid_mag, eid_cal, eid_cm = _setup_graph(graph_conn)
    related = get_related(graph_conn, eid_dv, depth=2)
    # Items at depth 1 vs depth 2 should be labeled correctly
    depths = {r["name"]: r["depth"] for r in related}
    assert depths.get("magnesium") == 1
    assert depths.get("calcium") == 2


def test_get_related_no_duplicates(graph_conn):
    eid_dv, eid_mag, eid_cal, eid_cm = _setup_graph(graph_conn)
    related = get_related(graph_conn, eid_mag, depth=2)
    ids = [r["entity_id"] for r in related]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# search_entities tests
# ---------------------------------------------------------------------------

def test_search_entities_fts(graph_conn):
    upsert_entity(graph_conn, "magnesium", "substance", "mineral essential for muscle function")
    upsert_entity(graph_conn, "calcium", "substance", "mineral for bone structure")
    upsert_entity(graph_conn, "cold thermogenesis", "protocol", "cold exposure protocol")

    results = search_entities(graph_conn, "mineral absorption")
    assert len(results) > 0
    names = [r["name"] for r in results]
    # Both mineral-related entities should appear
    assert any(n in ("magnesium", "calcium") for n in names)


def test_search_entities_respects_limit(graph_conn):
    for i in range(10):
        upsert_entity(graph_conn, f"entity_{i}", "concept", f"description about minerals {i}")
    results = search_entities(graph_conn, "minerals", limit=3)
    assert len(results) <= 3


def test_search_entities_returns_scores(graph_conn):
    upsert_entity(graph_conn, "magnesium", "substance", "mineral for muscle")
    results = search_entities(graph_conn, "mineral")
    if results:
        assert "score" in results[0]
        assert isinstance(results[0]["score"], float)


def test_search_entities_no_crash_on_empty_db(graph_conn):
    results = search_entities(graph_conn, "magnesium")
    assert results == []


# ---------------------------------------------------------------------------
# Completion signal test (matches plan's QA gate)
# ---------------------------------------------------------------------------

def test_completion_signal(tmp_path):
    """Replicate the plan's completion signal test exactly."""
    db = str(tmp_path / "test.db")
    conn = ensure_graph_schema(db)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "entities" in tables
    assert "relationships" in tables
    assert "entity_sources" in tables
    assert "entity_aliases" in tables
    conn.close()
