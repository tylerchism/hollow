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

---

## [2026-04-04] Extend Hard Prohibitions to cover ALL agents' behavioral documents

**Requested by**: Forge (on behalf of task A770G03 + wWl4AK8Q3HqVcKDqH0QR7)  
**Gated file**: `agents/tarn/soul.md`  
**Change type**: edit  
**Current content**:  
> "Any edit to soul.md, identity.md, agents.md, or crons.json is self-modification and MUST go through Claude Code or Forge."  
> "Gated files are: `agents/tarn/soul.md`, `agents/tarn/identity.md`, `agent-memory/tarn/agents.md`, `agent-memory/tarn/crons.json`, and `agent-memory/*/memory.md`"  
**Proposed content**: Extend both the prohibition text and the gated files list to cover all agents (any `agents/*/soul.md`, any `agents/*/identity.md`, any `agent-memory/*/agents.md`, any `agent-memory/*/crons.json`).  
**Reason**: Tarn edited Tap's soul.md via direct route (5th consecutive violation). The gate must cover cross-agent behavioral docs, not just Tarn's own files.  
**Status**: applied  
**Proposed**: 2026-04-04  
**Applied by**: Forge (Claude Code) 2026-04-04

---

## [2026-04-04] Extend delegation rules in agents.md for cross-agent self-modification

**Requested by**: Forge (on behalf of task A770G03 + wWl4AK8Q3HqVcKDqH0QR7)  
**Gated file**: `agent-memory/tarn/agents.md`  
**Change type**: edit  
**Current content**: Self-modification delegation rule covers only "Tarn self-modification (soul.md, agents.md, routing rules, protocol changes)"  
**Proposed content**: Extend to explicitly cover editing any other agent's soul.md, identity.md, agents.md, crons.json — all route through proposed_changes.md → Forge.  
**Reason**: Same violation pattern — rule was scoped too narrowly to Tarn's own files.  
**Status**: applied  
**Proposed**: 2026-04-04  
**Applied by**: Forge (Claude Code) 2026-04-04

## [2026-04-04] Test: Clarify soul.md Agent tool rule

**Requested by**: Tarn  
**Gated file**: `agents/tarn/soul.md`  
**Change**: In the "Subagents and Context Budget" section, add a clarifying sentence after "The specialist team doesn't go away — the subagent is the execution layer, not a bypass." Add: "Subagents call hail to reach specialists just as Tarn would; the routing rules apply equally."  
**Reason**: Clarify that routing rules still apply inside subagents — prevents confusion about whether subagent context changes the routing obligation.  
**Status**: applied  
**Proposed**: 2026-04-04  
**Applied by**: Forge (Claude Code) 2026-04-04
