# Tools — Flux

CLI tools available in `~/git/hollow/bin/`. Call all of these via Bash.

## Core Tools

### `hail`
Delegate to a specialist agent.
```bash
hail forge "build the RSI engine with these specs: ..."
hail tap "research funding rate arbitrage on Binance — what are the typical spreads and risks?"
hail briar "stress-test this strategy: ..."
hail canopy "synthesize the macro picture for BTC given these signals: ..."
```

Available agents: `forge` (builder), `tap` (deep research), `briar` (adversarial review), `canopy` (synthesis/framing), `spring` (content), `tarn` (coordinator).

### `mc`
Mission Control task board.
```bash
mc tasks list
mc tasks create "title" --priority=high
mc tasks update <id> --status=done
mc activity log "completed: X | route: Y | summary: Z"
```

### `xsearch`
xAI/Grok search — best for real-time market news, X/Twitter, current events.
```bash
xsearch "BTC funding rate arbitrage 2026"
xsearch "Binance futures basis trade"
```

### `discord-history`
Read recent messages from any Discord channel.
```bash
discord-history trader-bot        # #trader-bot channel
discord-history trading           # #trading channel
discord-history log-macro-arb     # #log-macro-arb
discord-history trading-summary   # #trading-summary
```
Use this to check what Tyler or Tarn posted, read webhook confirms, or get context from a channel you weren't active in.

### `send_discord_channel`
Post a message to a named Discord channel (one-way, no response).
```bash
send_discord_channel "trading-summary" "Daily P&L: +$142 | BTC RSI trade x3 | Max drawdown 0.4%"
send_discord_channel "log-crypto-arb" "Signal fired: BTC/USDT spread 0.18% — entered"
```

### `send_msg`
Post to the currently active channel (Discord or Telegram — wherever Tyler is messaging you from). Use for interim updates during long tasks.
```bash
send_msg "researching funding rate windows on Binance — back in a moment"
```

## Specialist Routing Rules (Flux-specific)

- **Need something built** → `hail forge`
- **Need deep research on a strategy or market mechanism** → `hail tap`
- **Stress-testing a strategy or risk profile** → `hail briar`
- **Macro synthesis or framing a thesis for Tyler** → `hail canopy`
- **Coordination, Discord setup, task routing** → `hail tarn` (or just tell Tyler)

## Memory

Your persistent memory lives in `~/git/hollow/agent-memory/flux/`. Read `memory.md` at session start to get current on what strategies are live, what's been tried, and what's in flight.
