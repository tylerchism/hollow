"""Top-level memory system API."""

import logging
from pathlib import Path

from src.config import Config

from .embeddings import EmbeddingClient, OllamaEmbeddingClient
from .indexer import IndexResult, Indexer
from .schema import ensure_schema
from .search import SearchResult, Searcher
from .session import SessionLogger

log = logging.getLogger(__name__)

# Core files loaded into every agent context
ALWAYS_LOAD = ["soul.md", "identity.md", "tools.md"]
# Files only loaded in main sessions
MAIN_SESSION_ONLY = ["memory.md", "project_state.md", "heartbeat.md"]

# Agents that receive Tyler's full user profile (routing + voice-aware agents)
_USER_PROFILE_AGENTS: frozenset[str] = frozenset({"tarn", "spring"})
# Agents that receive the team roster / routing table (coordinators only)
_AGENTS_ROSTER_AGENTS: frozenset[str] = frozenset({"tarn"})


class MemoryManager:
    """Orchestrates indexing, search, session logging, and context assembly."""

    def __init__(self, config: Config):
        self.config = config
        self.conn = None
        self.indexer: Indexer | None = None
        self.searcher: Searcher | None = None
        self.session_logger: SessionLogger | None = None
        self.embedding_client: EmbeddingClient | OllamaEmbeddingClient | None = None
        self._skills_summary: str = ""  # set externally after skills load

    async def initialize(self) -> IndexResult:
        """Create DB, set up components, run initial index. Returns index stats."""
        db_path = self.config.data_dir / "memory.db"
        self.conn = ensure_schema(db_path)

        # Set up embedding client: prefer ollama local
        if self.config.ollama_embed_model:
            self.embedding_client = OllamaEmbeddingClient(
                model=self.config.ollama_embed_model,
                dimensions=768,
            )
            log.info("Using Ollama embeddings: %s", self.config.ollama_embed_model)
        else:
            log.info("No embedding client configured — using keyword-only search")

        self.indexer = Indexer(
            conn=self.conn,
            memory_dir=self.config.memory_dir,
            embedding_client=self.embedding_client,
        )

        self.searcher = Searcher(
            conn=self.conn,
            embedding_client=self.embedding_client,
        )

        self.session_logger = SessionLogger(
            sessions_dir=self.config.memory_dir / "sessions",
            timezone_name=self.config.user_timezone,
        )

        # Initial index
        result = await self.indexer.index_all()
        log.info(
            "Indexed %d files (%d chunks), skipped %d unchanged",
            result.files_indexed,
            result.chunks_created,
            result.files_skipped,
        )
        return result

    async def get_context(
        self, query: str | None = None, is_main_session: bool = True
    ) -> str:
        """Assemble full context string for the agent."""
        parts = []

        # Determine agent identity for role-based file loading.
        # Falls back to loading everything if identity_dir is not set (dev/test safety).
        agent_name = self.config.identity_dir.name if self.config.identity_dir else ""

        # Core files
        files_to_load = list(ALWAYS_LOAD)
        # user.md — Tyler's profile — only for coordinator (routing) and Spring (voice)
        if not agent_name or agent_name in _USER_PROFILE_AGENTS:
            files_to_load.append("user.md")
        # agents.md — team roster — only for coordinator (Tarn)
        if not agent_name or agent_name in _AGENTS_ROSTER_AGENTS:
            files_to_load.append("agents.md")
        if is_main_session:
            files_to_load.extend(MAIN_SESSION_ONLY)

        for filename in files_to_load:
            content = self.read_file(filename)
            if content:
                parts.append(f'<file name="{filename}">\n{content}\n</file>')

        # NOTE: Session logs are NOT injected into context.
        # Hollow uses SQLite conversation history instead.
        # The session logger still WRITES logs for debugging purposes.

        # Wiki memory index — available to all agents in main sessions
        if is_main_session:
            wiki_dir = self.config.memory_dir.parent / "wiki"
            wiki_index = wiki_dir / "MEMORY.md"
            if wiki_index.exists():
                content = wiki_index.read_text()
                parts.append(f'<file name="wiki/MEMORY.md">\n{content}\n</file>')

        # Native Claude CLI tools (always available via bypassPermissions)
        parts.append(
            "<native_tools>\n"
            "Bash, Read, Write, Edit, Glob, Grep — all enabled with bypassPermissions.\n"
            "Use these for shell commands, file system operations, and code search.\n"
            "</native_tools>"
        )

        # Available MCP skills
        if self._skills_summary:
            parts.append(
                f"<available_skills>\n{self._skills_summary}\n</available_skills>"
            )

        # Search results for query
        if query and self.searcher:
            results = await self.search(query, limit=5)
            if results:
                search_text = "\n\n".join(
                    f"[{r.file_path}:{r.start_line}-{r.end_line}] (score: {r.score:.2f})\n{r.text}"
                    for r in results
                )
                parts.append(
                    f'<search_results query="{query}">\n{search_text}\n</search_results>'
                )

        return "\n\n".join(parts)

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Search indexed memory."""
        if not self.searcher:
            return []
        return await self.searcher.search(query, limit=limit)

    async def reindex(self, force: bool = False) -> IndexResult:
        """Re-index changed files."""
        if not self.indexer:
            return IndexResult()
        return await self.indexer.index_all(force=force)

    def log_message(self, role: str, content: str) -> None:
        """Append a message to today's session log."""
        if self.session_logger:
            self.session_logger.log_message(role, content)

    def read_file(self, filename: str) -> str | None:
        """Read a file from the memory directory.

        For soul.md and identity.md, checks identity_dir first (if configured).
        Strips YAML frontmatter (--- ... ---) before returning content to LLM context.
        """
        if filename in ("soul.md", "identity.md", "tools.md") and self.config.identity_dir:
            override = self.config.identity_dir / filename
            if override.exists():
                return self._strip_frontmatter(override.read_text(encoding="utf-8"))
        path = self.config.memory_dir / filename
        if path.exists():
            return self._strip_frontmatter(path.read_text(encoding="utf-8"))
        return None

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        """Strip YAML frontmatter from file content.

        If the content starts with '---\\n', finds the closing '---\\n' and
        returns the content after it. Otherwise returns content unchanged.
        """
        if not content.startswith("---\n"):
            return content
        end = content.find("---\n", 4)
        if end == -1:
            return content
        return content[end + 4:]

    async def close(self) -> None:
        """Clean up resources."""
        if self.embedding_client:
            await self.embedding_client.close()
        if self.conn:
            self.conn.close()
