## SPIKE: Per-Person Vector Knowledge Bases — Tech Selection + Interface Definition

**Project:** `qx12Fl` — Subtask 1 of 7 | **For:** Tyler review before build starts | **Date:** 2026-03-21

---

### What This Solves (and Doesn't)

This builds a **reference library**: retrieval of exact quotes, arguments, and sourced excerpts from specific thinkers. It does not solve voice synthesis or impersonation — those are a separate problem. The output is always: excerpt + attribution + source. Briar's attribution drift risk is real; the solution is to never return a result that isn't a direct chunk of ingested text.

---

### DB Recommendation: Extend Existing SQLite Infrastructure (Not Chroma / Qdrant / pgvector)

**Short version:** Hollow already has a production-grade hybrid vector search system. The expert corpus should run on the same stack.

**What already exists** (`src/memory/`):
- SQLite + WAL mode, per-agent at `/path/to/hollow/data/<agent>/memory.db`
- Paragraph-level chunking with hash-based change detection
- Hybrid search: FTS5 BM25 (30%) + cosine similarity (70%), normalized + deduplicated
- Embedding clients: Ollama local (`nomic-embed-text`, 768-dim) + Voyage API fallback
- Embedding cache by content hash (no redundant re-embedding)

**Per-person DB path convention:** `/path/to/hollow/data/corpus/<person_slug>/memory.db`

Each person gets their own isolated SQLite DB — same schema, separate file. Adding a new person is instantiating a new DB at a new path and running ingestion against it. No schema changes required.

**Why not Chroma/Qdrant/pgvector:**
- Chroma/Qdrant: new infrastructure to run and maintain, new dependencies, no meaningful capability gain at this corpus scale (hundreds of documents, not millions)
- pgvector: requires running Postgres, adds operational overhead, vector search performance advantage doesn't matter here
- The existing system already does what the use case needs. Introducing a second retrieval stack creates exactly the two-system problem the QK0S coordination note is trying to prevent

**Only add a dedicated vector DB if:** corpus scale grows to hundreds of thousands of chunks and SQLite write contention or query latency become observable problems. That's not today's problem.

---

### Retrieval Interface

**Call chain:** Tarn → hail tap → Tap invokes `retrieve` via Bash → returns results inline

Per Canopy's guidance, retrieval trigger lives in Tap (not Tarn directly). Tarn delegates research tasks to Tap; Tap calls the retrieval tool as part of its research process. Spring can also invoke retrieval when sourcing voice examples.

**CLI tool:** `bin/retrieve`

```bash
retrieve --query "soil carbon sequestration" \
         --person "paul-wheaton" \
         --source-type "transcript" \
         --after "2022-01-01" \
         --top-k 5
```

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `--query` | string | yes | Natural language query |
| `--person` | string | no | Slug (e.g., `paul-wheaton`); omit for cross-corpus |
| `--source-type` | string | no | `blog`, `transcript`, `newsletter`, `podcast` |
| `--after` | ISO date | no | Exclude older content |
| `--before` | ISO date | no | Exclude newer content |
| `--top-k` | int | no | Default: 5 |
| `--corpus` | enum | no | `expert` (default) or `archivist` (see QK0S note) |

**Return format (stdout JSON):**

```json
{
  "query": "soil carbon sequestration",
  "corpus": "expert",
  "results": [
    {
      "excerpt": "The actual quoted text chunk here...",
      "person": "paul-wheaton",
      "source_type": "transcript",
      "source_title": "Permaculture Podcast Ep. 412",
      "source_url": "https://...",
      "date": "2023-06-15",
      "platform": "youtube",
      "relevance_score": 0.91,
      "chunk_id": "sha256-prefix"
    }
  ]
}
```

**What agents do with this:** Tap cites directly. Spring pulls voice examples. Neither synthesizes attribution — the excerpt *is* the source. If no high-confidence results return, agents say so rather than filling the gap with inference.

---

### Chunking Strategy

Canopy's guidance is correct: chunk by argument unit, not fixed token size.

- Split on paragraph boundaries (existing chunker does this)
- Target 200–400 tokens per chunk; respect natural breaks over hitting a number
- Each chunk must be independently quotable without needing surrounding context
- Store full metadata on every chunk: person, source, date, platform, URL
- This is Briar's call: chunking strategy determines retrieval quality. Define it now, not after first ingestion

One metadata field worth adding that doesn't exist in the current agent memory schema: `platform` (blog, youtube, podcast, newsletter). Source type alone isn't enough — a transcript from YouTube and a transcript from a podcast have different reliability and context signals.

---

### QK0S Coordination — Shared Layer Definition

QK0S (Archivist) and qx12Fl (expert corpora) both need retrieval. They are different content but the same infrastructure problem.

**Resolution:** One retrieval tool (`bin/retrieve`), two corpus targets:

| `--corpus` | DB path | Content |
|---|---|---|
| `expert` (default) | `/path/to/hollow/data/corpus/<person_slug>/memory.db` | External thinker corpora |
| `archivist` | `/path/to/hollow/data/archivist/memory.db` | Hollow's own outputs, task logs, retro findings |

QK0S's three open spec questions resolve naturally under this model:
- **(a) Tarn query format:** Same interface, `--corpus archivist`
- **(b) Archivist return format:** Same JSON schema as above
- **(c) What gets indexed:** Hollow agent-activity outputs, task completions, retro findings — the Archivist ingestion pipeline decides this, not the retrieval interface

Build the retrieve tool once. Archivist plugs into the same system. This is the shared layer.

---

### Corpus Acquisition — Pre-Build Check

Before ingestion pipeline, confirm source availability per target person. Likely sources by viability:

| Source type | Viability | Notes |
|---|---|---|
| Personal blog / website | ✅ High | Static HTML scrape; usually permissive |
| Newsletter (Substack etc.) | ✅ High | Archive pages scrapeable |
| YouTube transcripts | ✅ High | `yt-dlp` + auto-captions or manual transcripts |
| Podcast transcripts | ⚠️ Medium | Need transcription (Whisper) or third-party transcripts |
| Books | ⚠️ Low | Copyright; fair use fragments only |
| Twitter/X | ❌ Low | API expensive; scraping fragile |

**PoC recommendation:** Pick one person with a strong blog or Substack + YouTube presence. That's two source types, both high-viability, no transcription overhead for blog. Avoid podcasts and books for the first pass.

---

### Staleness — Design Decision

The ingestion pipeline must be rerunnable, not a one-time index. The existing indexer already handles this (file hash tracking, re-index on change). For web sources, ingestion should be scheduled as a recurring cron, not run once and forgotten. Cadence defined at Subtask 3.

---

### Decisions Tyler Needs to Make Before Build Starts

1. **Who is the first person?** One person, one source type for PoC. Pick someone with a strong blog or Substack. This locks corpus scope for Subtask 2.
2. **Confirm retrieval trigger in Tap** — Canopy's guidance accepted? If yes, Forge specs Tap's tool invocation in Subtask 4. If Tarn should call retrieval directly, that changes the interface wiring.
3. **QK0S shared layer** — Confirm: one `retrieve` tool, two corpus targets. Archivist does not get a separate retrieval implementation.

---

*Forge — 2026-03-21*
