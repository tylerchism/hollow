"""Knowledge graph — entity/relationship store for cross-source linking.

Builds and queries the entity graph at ~/git/hollow/memory/knowledge-graph.db.
Schema: entities, relationships, entity_sources, entity_aliases + FTS5 index.
"""

import logging
import sqlite3
import struct
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

GRAPH_DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    description TEXT,
    embedding BLOB,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name_type ON entities(name, type);

CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_entity INTEGER NOT NULL REFERENCES entities(id),
    to_entity INTEGER NOT NULL REFERENCES entities(id),
    label TEXT NOT NULL,
    context TEXT,
    weight REAL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_entity);
CREATE INDEX IF NOT EXISTS idx_rel_to ON relationships(to_entity);

CREATE TABLE IF NOT EXISTS entity_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_db TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entity_sources_entity ON entity_sources(entity_id);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    alias TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alias ON entity_aliases(alias);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
    name, description, content='entities', content_rowid='id'
);

-- Triggers to keep FTS in sync with entities table
CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entities_fts(rowid, name, description)
    VALUES (new.id, new.name, COALESCE(new.description, ''));
END;

CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, description)
    VALUES ('delete', old.id, old.name, COALESCE(old.description, ''));
END;

CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
    INSERT INTO entities_fts(entities_fts, rowid, name, description)
    VALUES ('delete', old.id, old.name, COALESCE(old.description, ''));
    INSERT INTO entities_fts(rowid, name, description)
    VALUES (new.id, new.name, COALESCE(new.description, ''));
END;
"""

# Default path for the knowledge graph DB
DEFAULT_GRAPH_DB = Path("~/git/hollow/memory/knowledge-graph.db").expanduser()


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------

def ensure_graph_schema(db_path: str | Path) -> sqlite3.Connection:
    """Create/open the knowledge graph DB with WAL mode and FKs enabled.

    Follows the same pattern as src/memory/schema.py:ensure_schema().
    Idempotent — safe to call on an existing DB.

    Returns an open sqlite3.Connection with row_factory=sqlite3.Row.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(GRAPH_DDL)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def upsert_entity(
    conn: sqlite3.Connection,
    name: str,
    entity_type: str,
    description: str | None = None,
    embedding: list[float] | None = None,
) -> int:
    """Insert or update an entity. Returns the entity ID.

    If an entity with (name, type) already exists:
    - Updates description if the new one is non-None and non-empty
    - Updates embedding if provided
    - Updates updated_at
    """
    emb_blob = _embedding_to_blob(embedding) if embedding else None

    existing = conn.execute(
        "SELECT id FROM entities WHERE name = ? AND type = ?",
        (name, entity_type),
    ).fetchone()

    if existing:
        entity_id = existing["id"]
        if description or emb_blob:
            conn.execute(
                """UPDATE entities
                   SET description = COALESCE(?, description),
                       embedding   = COALESCE(?, embedding),
                       updated_at  = datetime('now')
                   WHERE id = ?""",
                (description, emb_blob, entity_id),
            )
            conn.commit()
        return entity_id

    cursor = conn.execute(
        """INSERT INTO entities (name, type, description, embedding)
           VALUES (?, ?, ?, ?)""",
        (name, entity_type, description, emb_blob),
    )
    conn.commit()
    return cursor.lastrowid


def add_relationship(
    conn: sqlite3.Connection,
    from_id: int,
    to_id: int,
    label: str,
    context: str | None = None,
    weight: float = 1.0,
) -> int:
    """Insert a directed relationship edge. Returns relationship ID."""
    cursor = conn.execute(
        """INSERT INTO relationships (from_entity, to_entity, label, context, weight)
           VALUES (?, ?, ?, ?, ?)""",
        (from_id, to_id, label, context, weight),
    )
    conn.commit()
    return cursor.lastrowid


def add_source(
    conn: sqlite3.Connection,
    entity_id: int,
    source_type: str,
    source_id: str,
    source_db: str | None = None,
) -> None:
    """Record a source link for an entity. Silently skips exact duplicates."""
    existing = conn.execute(
        """SELECT id FROM entity_sources
           WHERE entity_id = ? AND source_type = ? AND source_id = ?""",
        (entity_id, source_type, source_id),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """INSERT INTO entity_sources (entity_id, source_type, source_id, source_db)
           VALUES (?, ?, ?, ?)""",
        (entity_id, source_type, source_id, source_db),
    )
    conn.commit()


def add_alias(conn: sqlite3.Connection, entity_id: int, alias: str) -> None:
    """Add an alias for an entity. Silently skips if the alias already exists."""
    try:
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (entity_id, alias),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Alias already mapped — ignore
        pass


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def find_entity(conn: sqlite3.Connection, name: str) -> dict | None:
    """Find an entity by its canonical name OR any of its aliases.

    Returns a dict of entity columns, or None if not found.
    """
    # Check canonical name first (case-insensitive)
    row = conn.execute(
        "SELECT * FROM entities WHERE name = ? COLLATE NOCASE",
        (name,),
    ).fetchone()
    if row:
        return dict(row)

    # Check aliases
    alias_row = conn.execute(
        """SELECT e.* FROM entities e
           JOIN entity_aliases a ON a.entity_id = e.id
           WHERE a.alias = ? COLLATE NOCASE""",
        (name,),
    ).fetchone()
    if alias_row:
        return dict(alias_row)

    return None


def get_related(
    conn: sqlite3.Connection,
    entity_id: int,
    depth: int = 1,
) -> list[dict]:
    """BFS traversal of the relationship graph up to `depth` hops.

    Returns a list of dicts, each with keys:
        entity_id, name, type, description, label, context, weight, depth
    """
    visited: set[int] = {entity_id}
    frontier: list[tuple[int, int]] = [(entity_id, 0)]  # (id, current_depth)
    results: list[dict] = []

    while frontier:
        current_id, current_depth = frontier.pop(0)
        if current_depth >= depth:
            continue

        rows = conn.execute(
            """SELECT e.id, e.name, e.type, e.description,
                      r.label, r.context, r.weight
               FROM relationships r
               JOIN entities e ON (
                   CASE WHEN r.from_entity = ? THEN r.to_entity
                        ELSE r.from_entity END = e.id
               )
               WHERE r.from_entity = ? OR r.to_entity = ?""",
            (current_id, current_id, current_id),
        ).fetchall()

        for row in rows:
            neighbor_id = row["id"]
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)
            results.append(
                {
                    "entity_id": neighbor_id,
                    "name": row["name"],
                    "type": row["type"],
                    "description": row["description"],
                    "label": row["label"],
                    "context": row["context"],
                    "weight": row["weight"],
                    "depth": current_depth + 1,
                }
            )
            frontier.append((neighbor_id, current_depth + 1))

    return results


def search_entities(
    conn: sqlite3.Connection,
    query: str,
    embedding_client=None,
    limit: int = 10,
) -> list[dict]:
    """Hybrid FTS5 + vector search over entity name and description.

    Falls back to keyword-only if embedding_client is None or unavailable.
    Returns a list of dicts with entity columns + score.
    """
    import asyncio
    import math

    # -- Keyword search via FTS5 --
    fts_results: list[dict] = []
    try:
        # Use OR semantics so multi-word queries match any term
        clean_tokens = [f'"{t}"' for t in query.split() if t]
        fts_query = " OR ".join(clean_tokens) if clean_tokens else '""'
        rows = conn.execute(
            """SELECT e.id, e.name, e.type, e.description, e.embedding,
                      rank AS bm25_score
               FROM entities_fts fts
               JOIN entities e ON e.id = fts.rowid
               WHERE entities_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (fts_query, limit * 2),
        ).fetchall()
        for row in rows:
            fts_results.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "type": row["type"],
                    "description": row["description"],
                    "embedding": row["embedding"],
                    "score": -row["bm25_score"],  # BM25 is negative, flip for consistency
                }
            )
    except Exception as e:
        log.warning("Entity FTS search failed: %s", e)

    # -- Vector search (optional) --
    vector_results: list[dict] = []
    if embedding_client is not None:
        try:
            loop = asyncio.new_event_loop()
            try:
                query_emb = loop.run_until_complete(
                    embedding_client.embed_single(query)
                )
            finally:
                loop.close()

            all_rows = conn.execute(
                "SELECT id, name, type, description, embedding FROM entities WHERE embedding IS NOT NULL"
            ).fetchall()
            for row in all_rows:
                emb = _blob_to_embedding(row["embedding"])
                sim = _cosine_similarity(query_emb, emb)
                vector_results.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "type": row["type"],
                        "description": row["description"],
                        "embedding": row["embedding"],
                        "score": sim,
                    }
                )
            vector_results.sort(key=lambda x: x["score"], reverse=True)
            vector_results = vector_results[:limit * 2]
        except Exception as e:
            log.warning("Entity vector search failed: %s", e)

    # -- Merge --
    _normalize_scores(fts_results)
    _normalize_scores(vector_results)

    if vector_results:
        kw_weight, vec_weight = 0.3, 0.7
    else:
        kw_weight, vec_weight = 1.0, 0.0

    merged: dict[int, dict] = {}
    for r in fts_results:
        merged[r["id"]] = {**r, "keyword_score": r["score"], "vector_score": 0.0}
    for r in vector_results:
        if r["id"] in merged:
            merged[r["id"]]["vector_score"] = r["score"]
        else:
            merged[r["id"]] = {**r, "keyword_score": 0.0, "vector_score": r["score"]}

    final = []
    for item in merged.values():
        combined = kw_weight * item["keyword_score"] + vec_weight * item["vector_score"]
        final.append(
            {
                "id": item["id"],
                "name": item["name"],
                "type": item["type"],
                "description": item["description"],
                "score": combined,
            }
        )

    final.sort(key=lambda x: x["score"], reverse=True)
    return final[:limit]


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _embedding_to_blob(embedding: list[float]) -> bytes:
    return struct.pack(f"{len(embedding)}f", *embedding)


def _blob_to_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _normalize_scores(results: list[dict]) -> None:
    """Min-max normalize the 'score' field in-place."""
    if not results:
        return
    scores = [r["score"] for r in results]
    min_s, max_s = min(scores), max(scores)
    spread = max_s - min_s
    if spread == 0:
        for r in results:
            r["score"] = 1.0 if max_s > 0 else 0.0
    else:
        for r in results:
            r["score"] = (r["score"] - min_s) / spread


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
