# Hollow

Hollow is a multi-agent AI orchestration system where adversarial review is a first-class feature. Specialist agents are designed to disagree with each other — a dedicated critic (Briar) stress-tests every plan, a researcher (Tap) demands primary sources, and a coordinator (Tarn) routes tasks without becoming a bottleneck. The system runs locally, persists conversation history across restarts, and is configured entirely through a setup wizard.

## System Requirements

**Hardware:**

- **RAM:** 8GB minimum, 16GB comfortable. Running 5+ Claude agents concurrently needs headroom.
- **CPU:** No GPU required. Ollama (local embeddings) runs fine on CPU, just slower on larger models.
- **Storage:** ~500MB for Python deps + ~275MB for the Ollama embedding model if used.
- **OS:** Linux or macOS. (Windows via WSL2 should work but is untested.)
- **Internet:** Required — all agents call the Claude API for every response.

**Required:**

- **Claude MAX subscription** — Hollow uses the `claude` CLI via the Claude Code SDK, not a bare API key. A standard API key is not sufficient. You must have Claude MAX.
- **`claude` CLI installed and authenticated** — Install from [claude.ai/code](https://claude.ai/code) and run `claude` once to authenticate before running Hollow.
- **Python 3.11+**
- **`git`**

**Auto-installed by `install.sh`:**

- **`uv`** — Python package manager
- **`gh` CLI** — GitHub repo operations from inside agents
- **Ollama** — Local vector embeddings (optional; falls back to keyword-only memory if skipped)
- **`nomic-embed-text`** — Ollama embedding model (~275MB, optional)
- **All Python dependencies** from `pyproject.toml`

**Optional (prompted during setup):**

- **Telegram bot token** — For notifications and mobile access. Create via [@BotFather](https://t.me/BotFather).
- **xAI API key** — For Grok/X search via the `xsearch` tool. Get at [console.x.ai](https://console.x.ai).

## Memory: Embeddings

Hollow uses a hybrid vector + keyword memory system backed by SQLite:

- **With Ollama** — `nomic-embed-text` runs locally, no external API needed. Semantic search weighted 70%, keyword 30%. Runs on CPU (no GPU required).
- **With Voyage API** — Cloud-based embeddings if you have a Voyage key. Set `VOYAGE_API_KEY` in `.env`.
- **Neither configured** — Falls back to keyword-only (FTS5). Still functional for most use cases.

The linear vector scan works well up to ~50K memory chunks. At that scale and beyond, a proper index would help — but you'd have to accumulate months of heavy usage to get there.

## Installation

```bash
git clone https://github.com/tylerchism/hollow
cd hollow
./install.sh
```

`install.sh` handles everything: installs `uv`, `gh`, Ollama (optional), pulls the embedding model, installs Python dependencies, then launches the interactive setup wizard.

The setup wizard will:
- Ask for your name, timezone, and team configuration
- Scan for available ports automatically (no hardcoded ports)
- Prompt for API keys (masked display, never overwrites without confirmation)
- Generate `hollow.config.json`, `.env`, agent memory directories, identity files, `bin/hail`, and `start.sh`
- Skip existing agent memory directories to preserve conversation history (idempotent — safe to re-run)

After setup:

```bash
./start.sh
```

## Default Team Structure

The default team is five specialist agents plus a coordinator. Each role exists because no single model does all of these well simultaneously.

| Agent | Role | Why it exists |
|-------|------|---------------|
| **Tarn** (coordinator) | Routes tasks, holds state, primary user interface | Prevents cognitive load from hitting the user — one entry point for everything |
| **Tap** | Deep research, empirical depth, citations | Demands primary sources and distinguishes strong evidence from weak; generalists cut corners |
| **Canopy** | Cross-domain synthesis, strategic framing | Finds the non-obvious parallel; pure researchers stay too narrow |
| **Briar** | Adversarial review, stress-testing plans | Finds the failure mode before you ship; without a dedicated critic, plans get approved by the team that wrote them |
| **Forge** | Project scoping, build planning, GitHub-native | Translates ideas into sequenced, dependency-aware work; prevents scope creep |
| **Spring** | Creative writing, voice, content | Generic AI prose is flat; voice requires a dedicated focus |

Briar is not optional. An agent team without adversarial review is a yes-machine.

## Configuration

Hollow reads from `hollow.config.json` at runtime. The setup wizard generates this file. Example structure:

```json
{
  "version": "v1",
  "coordinator": {
    "name": "tarn",
    "port": 18800,
    "description": "Coordinator — primary user interface"
  },
  "agents": [
    {
      "name": "briar",
      "port": 18794,
      "role": "Adversarial review, risk analysis, stress-testing plans",
      "description": "...",
      "hail_keyword": "briar"
    }
  ],
  "user": {
    "name": "Tyler",
    "timezone": "America/Chicago"
  }
}
```

See `hollow.config.json.example` for the full default team. The live `hollow.config.json` is generated by `python setup.py` and is gitignored by default.

`bin/hail` reads this config at runtime — adding an agent to the config makes it immediately routable without restarting other agents:

```bash
hail briar "stress-test this deployment plan"
hail tap "what does the literature say about X?"
hail canopy "find the strategic angle here"
```

## Agent Memory

Each agent maintains persistent memory in `agent-memory/<name>/`. This directory is:
- **Never overwritten by setup.py** — re-running the wizard skips existing memory with a warning
- **Gitignored** — live conversation history should not be in version control

Agent identity files (soul, persona, routing rules) live in `agents/<name>/` and are generated once from templates in `templates/`.

## Optional Agent Patterns

Two patterns that are useful but not included in the default team:

**Memory steward** — A dedicated agent whose only job is to curate and compact long-term memory across the team. Useful once agents accumulate months of conversation history.

**User-proxy agent** — An agent that simulates how the user would respond to a draft, useful for content review loops. Can be configured with a custom soul describing the target audience.

Both can be added by running `python setup.py` and adding them to the agent roster, or by editing `hollow.config.json` directly and restarting.

## Project Structure

```
hollow/
  agents/              # Identity files (soul.md, identity.md) per agent
  agent-memory/        # Live conversation memory per agent (gitignored)
  bin/                 # CLI tools: hail, mc, xsearch
  data/                # SQLite databases, logs (gitignored)
  src/                 # Python runtime: agent, memory, channels, config
  templates/           # Template files for setup wizard rendering
  hollow.config.json   # Live config (generated by setup.py, gitignored)
  hollow.config.json.example  # Example config committed to repo
  setup.py             # Interactive setup wizard
  start.sh             # Launch all agents (generated or static)
  pyproject.toml       # Python dependencies (managed by uv)
```
