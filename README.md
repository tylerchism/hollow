# Hollow

Python agent runtime for Tarn and the specialist team. Successor to second_brain, built on claude_code_sdk (Claude MAX subscription).

## Agents

- **Tarn** (port 18800) — coordinator, Tyler-facing
- **Canopy** (port 18793) — cross-domain synthesis
- **Tap** (port 18792) — deep research, citations
- **Briar** (port 18794) — adversarial review
- **Forge** (port 18795) — project lead, builder
- **Spring** (port 18796) — creative writing, voice

## Running

```bash
uv run python -m src.main --port 18800 --identity-dir agents/tarn --memory-dir agent-memory/tarn
```

## Docs

- Spec: `~/git/mission-control/docs/hollow-spec.md`
- Plan: `~/git/mission-control/docs/hollow-plan.md`
