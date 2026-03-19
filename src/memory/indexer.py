"""File discovery, chunking, and SQLite indexing."""

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .embeddings import EmbeddingClient, embedding_to_blob

log = logging.getLogger(__name__)


@dataclass
class IndexResult:
    """Summary of an indexing run."""

    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    chunks_created: int = 0
    errors: list[str] = field(default_factory=list)


class Indexer:
    """Discovers, chunks, and indexes markdown files into SQLite."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        memory_dir: Path,
        embedding_client: EmbeddingClient | None = None,
        chunk_max_tokens: int = 400,
        chunk_overlap_tokens: int = 80,
    ):
        self.conn = conn
        self.memory_dir = memory_dir
        self.embedding_client = embedding_client
        self.chunk_max_tokens = chunk_max_tokens
        self.chunk_overlap_tokens = chunk_overlap_tokens

    def _discover_files(self) -> list[Path]:
        """Find all markdown files in memory directory."""
        return sorted(self.memory_dir.rglob("*.md"))

    def _file_hash(self, path: Path) -> str:
        """SHA-256 hash of file contents."""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _file_changed(self, path: Path, content_hash: str) -> bool:
        """Check if file has changed since last index."""
        row = self.conn.execute(
            "SELECT content_hash FROM files WHERE path = ?",
            (str(path),),
        ).fetchone()
        if row is None:
            return True
        return row["content_hash"] != content_hash

    def _chunk_markdown(self, text: str) -> list[dict]:
        """Split text into chunks on paragraph boundaries."""
        lines = text.split("\n")
        paragraphs = []
        current_lines: list[str] = []
        current_start = 1

        for i, line in enumerate(lines):
            line_num = i + 1
            if line.strip() == "" and current_lines:
                paragraphs.append(
                    {
                        "text": "\n".join(current_lines),
                        "start_line": current_start,
                        "end_line": line_num - 1,
                    }
                )
                current_lines = []
                current_start = line_num + 1
            else:
                if not current_lines:
                    current_start = line_num
                current_lines.append(line)

        if current_lines:
            paragraphs.append(
                {
                    "text": "\n".join(current_lines),
                    "start_line": current_start,
                    "end_line": len(lines),
                }
            )

        if not paragraphs:
            return []

        chunks = []
        current_chunk_parts: list[dict] = []
        current_token_count = 0

        for para in paragraphs:
            para_tokens = len(para["text"].split())

            if current_token_count + para_tokens > self.chunk_max_tokens and current_chunk_parts:
                chunks.append(self._merge_parts(current_chunk_parts))
                overlap_parts = []
                overlap_tokens = 0
                for part in reversed(current_chunk_parts):
                    pt = len(part["text"].split())
                    if overlap_tokens + pt > self.chunk_overlap_tokens:
                        break
                    overlap_parts.insert(0, part)
                    overlap_tokens += pt
                current_chunk_parts = overlap_parts
                current_token_count = overlap_tokens

            current_chunk_parts.append(para)
            current_token_count += para_tokens

        if current_chunk_parts:
            chunks.append(self._merge_parts(current_chunk_parts))

        return chunks

    def _merge_parts(self, parts: list[dict]) -> dict:
        """Merge paragraph parts into a single chunk."""
        return {
            "text": "\n\n".join(p["text"] for p in parts),
            "start_line": parts[0]["start_line"],
            "end_line": parts[-1]["end_line"],
        }

    def _upsert_file(self, path: Path, content_hash: str, chunks: list[dict]):
        """Transaction: delete old data, insert new chunks and file record."""
        path_str = str(path)
        now = datetime.now(timezone.utc).isoformat()

        with self.conn:
            self.conn.execute("DELETE FROM chunks WHERE file_path = ?", (path_str,))
            self.conn.execute("DELETE FROM files WHERE path = ?", (path_str,))

            self.conn.execute(
                "INSERT INTO files (path, content_hash, mtime, indexed_at) VALUES (?, ?, ?, ?)",
                (path_str, content_hash, path.stat().st_mtime, now),
            )

            for chunk in chunks:
                chunk_hash = hashlib.sha256(chunk["text"].encode()).hexdigest()
                self.conn.execute(
                    """INSERT INTO chunks (file_path, start_line, end_line, text, content_hash)
                    VALUES (?, ?, ?, ?, ?)""",
                    (path_str, chunk["start_line"], chunk["end_line"], chunk["text"], chunk_hash),
                )

    async def _embed_new_chunks(self):
        """Embed chunks that don't have embeddings yet, using cache."""
        if not self.embedding_client:
            return

        rows = self.conn.execute(
            "SELECT id, content_hash, text FROM chunks WHERE embedding IS NULL"
        ).fetchall()

        if not rows:
            return

        to_embed: list[tuple[int, str, str]] = []
        for row in rows:
            cached = self.conn.execute(
                "SELECT embedding FROM embedding_cache WHERE content_hash = ? AND model = ?",
                (row["content_hash"], self.embedding_client.model),
            ).fetchone()
            if cached:
                self.conn.execute(
                    "UPDATE chunks SET embedding = ? WHERE id = ?",
                    (cached["embedding"], row["id"]),
                )
            else:
                to_embed.append((row["id"], row["content_hash"], row["text"]))

        if not to_embed:
            self.conn.commit()
            return

        for i in range(0, len(to_embed), 128):
            batch = to_embed[i : i + 128]
            texts = [t[2] for t in batch]

            try:
                embeddings = await self.embedding_client.embed(texts)
            except Exception as e:
                log.warning("Embedding failed, continuing without vectors: %s", e)
                return

            for (chunk_id, content_hash, _text), emb in zip(batch, embeddings):
                blob = embedding_to_blob(emb)
                self.conn.execute(
                    "UPDATE chunks SET embedding = ? WHERE id = ?",
                    (blob, chunk_id),
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO embedding_cache (content_hash, model, embedding) VALUES (?, ?, ?)",
                    (content_hash, self.embedding_client.model, blob),
                )

        self.conn.commit()

    async def index_file(self, path: Path, force: bool = False) -> bool:
        """Index a single file. Returns True if file was (re)indexed."""
        if not path.exists():
            return False

        content_hash = self._file_hash(path)
        if not force and not self._file_changed(path, content_hash):
            return False

        text = path.read_text(encoding="utf-8")
        chunks = self._chunk_markdown(text)
        self._upsert_file(path, content_hash, chunks)
        return True

    async def index_all(self, force: bool = False) -> IndexResult:
        """Index all markdown files. Returns summary."""
        result = IndexResult()
        files = self._discover_files()
        result.files_scanned = len(files)

        for path in files:
            try:
                indexed = await self.index_file(path, force=force)
                if indexed:
                    result.files_indexed += 1
                    chunks = self.conn.execute(
                        "SELECT COUNT(*) as cnt FROM chunks WHERE file_path = ?",
                        (str(path),),
                    ).fetchone()
                    result.chunks_created += chunks["cnt"]
                else:
                    result.files_skipped += 1
            except Exception as e:
                log.error("Error indexing %s: %s", path, e)
                result.errors.append(f"{path}: {e}")

        try:
            await self._embed_new_chunks()
        except Exception as e:
            log.warning("Embedding pass failed: %s", e)

        return result
