# Knowledge Tools

These tools give you access to Hollow's unified knowledge base — everything Tyler has discussed, what the experts have said, and what the system has learned across sessions.

---

## bin/recall — Search Hollow's Knowledge Base

Search across all knowledge sources (wiki pages, expert corpus, entity graph) in one call.

```bash
bin/recall "your query here" [--top-k 5] [--sources wiki,corpus,graph] [--person SLUG]
```

**When to use:**
- You encounter a topic Tyler has likely discussed before
- You need to check what DV, CM, or BW have said about something
- You want to verify a fact before stating it
- You're connecting concepts across different sources
- You sense a knowledge gap — something you should know but don't

**When NOT to use:**
- Simple greetings or meta-conversation
- Topics clearly outside Hollow's knowledge domain
- When the answer is already in your loaded context

Returns JSON with ranked results from wiki, corpus, and entity graph.

**Examples:**
```bash
bin/recall "magnesium absorption"
bin/recall "Tyler trading decisions" --sources wiki
bin/recall "cold thermogenesis" --person daniel-vitalis --top-k 3
```

---

## bin/remember — Store Knowledge for the System

Write a structured memory entry that feeds into nightly consolidation.

```bash
bin/remember "text to remember" --type=TYPE [--source SOURCE] [--confidence high|medium|low]
```

**Types:** preference, decision, entity, relationship, fact, correction

**When to use:**
- Tyler states a preference or makes a decision
- You discover a new entity or connection worth tracking
- You learn a fact that future sessions should know
- A previously stored fact is now wrong (use --type=correction)

**When NOT to use:**
- Ephemeral conversation (small talk, transient questions)
- Information already in the wiki (check with recall first)
- Uncertain or speculative claims (unless --confidence=low)

**Examples:**
```bash
bin/remember "Tyler prefers bone broth over collagen supplements" --type=preference
bin/remember "Tyler decided to use SQLite over ChromaDB" --type=decision --source="task:knowledge-architecture"
bin/remember "Magnesium -- mineral involved in 300+ enzyme functions" --type=entity
bin/remember "Daniel Vitalis discusses cold thermogenesis as mitochondrial uncoupling" --type=relationship
bin/remember "Hollow corpus has 2835 DV chunks as of 2026-04-09" --type=fact
bin/remember "Tyler no longer follows strict carnivore — transitioned to ancestral mixed" --type=correction
```
