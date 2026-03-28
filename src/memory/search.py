"""Hybrid keyword + vector search over indexed chunks."""

import logging
import math
import re
import sqlite3
from dataclasses import dataclass

from .embeddings import EmbeddingClient, blob_to_embedding

log = logging.getLogger(__name__)

STOP_WORDS = frozenset(
    "a an and are as at be by for from has have i in is it its of on or "
    "that the to was were will with you your".split()
)


@dataclass
class SearchResult:
    """A single search result from hybrid search."""

    file_path: str
    start_line: int
    end_line: int
    text: str
    score: float
    keyword_score: float
    vector_score: float
    person: str | None = None
    platform: str | None = None
    source_type: str | None = None
    source_title: str | None = None
    source_url: str | None = None
    date: str | None = None


class Searcher:
    """Hybrid keyword (FTS5) + vector (cosine similarity) search."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        embedding_client: EmbeddingClient | None = None,
    ):
        self.conn = conn
        self.embedding_client = embedding_client

    def _clean_query(self, query: str) -> str:
        """Build a safe FTS5 query from natural language input."""
        tokens = re.findall(r"[a-zA-Z0-9]+", query)
        filtered = [t for t in tokens if t.lower() not in STOP_WORDS]
        if not filtered:
            filtered = tokens
        return " ".join(f'"{t}"' for t in filtered) if filtered else '""'

    def _keyword_search(self, query: str, limit: int) -> list[dict]:
        """FTS5 keyword search with BM25 ranking."""
        clean = self._clean_query(query)
        fts_query = clean.replace('"', '""')

        try:
            rows = self.conn.execute(
                """
                SELECT c.id, c.file_path, c.start_line, c.end_line, c.text,
                       c.person, c.platform, c.source_type, c.source_title,
                       c.source_url, c.date,
                       rank AS bm25_score
                FROM chunks_fts fts
                JOIN chunks c ON c.id = fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        except Exception as e:
            log.warning("FTS query failed for '%s': %s", fts_query, e)
            return []

        return [
            {
                "id": row["id"],
                "file_path": row["file_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "text": row["text"],
                "person": row["person"],
                "platform": row["platform"],
                "source_type": row["source_type"],
                "source_title": row["source_title"],
                "source_url": row["source_url"],
                "date": row["date"],
                "score": -row["bm25_score"],
            }
            for row in rows
        ]

    async def _vector_search(self, query: str, limit: int) -> list[dict]:
        """Vector search via cosine similarity on embeddings."""
        if not self.embedding_client:
            return []

        try:
            query_emb = await self.embedding_client.embed_single(query)
        except Exception as e:
            log.warning("Query embedding failed: %s", e)
            return []

        rows = self.conn.execute(
            """SELECT id, file_path, start_line, end_line, text, embedding,
                      person, platform, source_type, source_title, source_url, date
               FROM chunks WHERE embedding IS NOT NULL"""
        ).fetchall()

        if not rows:
            return []

        scored = []
        for row in rows:
            emb = blob_to_embedding(row["embedding"])
            sim = _cosine_similarity(query_emb, emb)
            scored.append(
                {
                    "id": row["id"],
                    "file_path": row["file_path"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "text": row["text"],
                    "person": row["person"],
                    "platform": row["platform"],
                    "source_type": row["source_type"],
                    "source_title": row["source_title"],
                    "source_url": row["source_url"],
                    "date": row["date"],
                    "score": sim,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def _merge_results(
        self,
        keyword_results: list[dict],
        vector_results: list[dict],
        keyword_weight: float,
        vector_weight: float,
        limit: int,
    ) -> list[SearchResult]:
        """Normalize scores, combine with weights, deduplicate."""
        _normalize_scores(keyword_results)
        _normalize_scores(vector_results)

        merged: dict[int, dict] = {}

        for r in keyword_results:
            merged[r["id"]] = {
                **r,
                "keyword_score": r["score"],
                "vector_score": 0.0,
            }

        for r in vector_results:
            if r["id"] in merged:
                merged[r["id"]]["vector_score"] = r["score"]
            else:
                merged[r["id"]] = {
                    **r,
                    "keyword_score": 0.0,
                    "vector_score": r["score"],
                }

        results = []
        for item in merged.values():
            combined = (
                keyword_weight * item["keyword_score"]
                + vector_weight * item["vector_score"]
            )
            results.append(
                SearchResult(
                    file_path=item["file_path"],
                    start_line=item["start_line"],
                    end_line=item["end_line"],
                    text=item["text"],
                    score=combined,
                    keyword_score=item["keyword_score"],
                    vector_score=item["vector_score"],
                    person=item.get("person"),
                    platform=item.get("platform"),
                    source_type=item.get("source_type"),
                    source_title=item.get("source_title"),
                    source_url=item.get("source_url"),
                    date=item.get("date"),
                )
            )

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:limit]

    async def search(
        self,
        query: str,
        limit: int = 10,
        keyword_weight: float = 0.3,
        vector_weight: float = 0.7,
    ) -> list[SearchResult]:
        """Hybrid search combining keyword and vector results."""
        keyword_results = self._keyword_search(query, limit=limit * 2)
        vector_results = await self._vector_search(query, limit=limit * 2)

        if not vector_results:
            keyword_weight = 1.0
            vector_weight = 0.0

        return self._merge_results(
            keyword_results, vector_results, keyword_weight, vector_weight, limit
        )


def _normalize_scores(results: list[dict]):
    """Min-max normalize scores in place."""
    if not results:
        return
    scores = [r["score"] for r in results]
    min_s = min(scores)
    max_s = max(scores)
    spread = max_s - min_s
    if spread == 0:
        for r in results:
            r["score"] = 1.0 if max_s > 0 else 0.0
    else:
        for r in results:
            r["score"] = (r["score"] - min_s) / spread


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
