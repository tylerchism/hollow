# Pending Restarts

## 2026-03-30 — Discord channel routing for Flux / #trader-bot

**Requires restart of:** Tarn (the Discord bot process)

**Why:** `src/channels/discord_channel.py` was updated to load channel routing from
`hollow.config.json` at startup and route `#trader-bot` messages to Flux (port 18799)
via its /ask endpoint. The routing map and webhook path for `send_discord` are only
loaded at process startup — a restart is required to pick up these changes.

**What changes are live after restart:**
- Messages in `#trader-bot` → forwarded to Flux at port 18799
- Flux's responses → posted back to `#trader-bot` via webhook as "Flux"
- `send_discord` / `send_msg` calls during a `#trader-bot` session → posted via
  webhook as "Flux" (not the bot user)

**Do NOT restart until Tyler explicitly requests it.**
