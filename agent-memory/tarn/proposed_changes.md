# Proposed Changes Queue

## Purpose

This file is the **required staging path** for any proposed modification to Tarn's gated files:
- `agents/tarn/soul.md`
- `agents/tarn/identity.md`
- `agent-memory/tarn/agents.md`
- `agent-memory/tarn/crons.json`
- `agent-memory/*/memory.md` (excluding operational continuity logs)

**Tarn never has the last write.** Tarn may request a change by appending an entry here with status `pending`. Forge (or a Claude Code subagent) must be the writing process that applies the change to the live file and sets status to `applied`.

Self-satisfaction bypass is closed: if Tarn writes the approval field itself and then applies the change, the gate is violated. Forge must own the Write/Edit call on live files.

---

## Format

Each entry must include:
- **Requested by:** Tarn (or agent name)
- **Target file:** exact path
- **Change type:** add | edit | delete
- **Current content:** (exact text being changed, or "N/A" for add)
- **Proposed content:** (exact replacement text)
- **Reason:** why this change is needed
- **Status:** pending | approved | applied | rejected

---

## Queue

(empty — no pending changes)
