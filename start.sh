#!/usr/bin/env bash
# start.sh — Launch the Hollow agent system
# Uses Python to parse hollow.config.json (no jq dependency)

set -euo pipefail

HOLLOW_ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$HOLLOW_ROOT/hollow.config.json"

if [ ! -f "$CONFIG" ]; then
    echo "Error: hollow.config.json not found at $CONFIG"
    echo "Run: python setup.py"
    exit 1
fi

# Use Python to parse config and check/launch agents
python3 - "$CONFIG" "$HOLLOW_ROOT" <<'PYEOF'
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

config_path = sys.argv[1]
hollow_root = Path(sys.argv[2])

with open(config_path) as f:
    cfg = json.load(f)

coord = cfg["coordinator"]
agents = cfg.get("agents", [])
user_tz = cfg.get("user", {}).get("timezone", "UTC")

procs = []


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.connect(("127.0.0.1", port))
            return True
        except ConnectionRefusedError:
            return False


_TOOL_NAME_MAP = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "agent": "Agent",
}


def normalize_tools(tools_list):
    """Convert config tool names (lowercase) to SDK tool names (capitalized)."""
    if not tools_list or tools_list == ["all"]:
        return ""
    result = []
    for t in tools_list:
        sdk_name = _TOOL_NAME_MAP.get(t.lower(), t)
        result.append(sdk_name)
    return ",".join(result)


def launch(name, port, identity_dir, memory_dir, data_dir, model=None, tools=None):
    if port_in_use(port):
        print(f"  WARNING: port {port} already in use — {name} may already be running")
        return None
    cmd = [
        "/home/tchism/.local/bin/uv", "run", "python", "-m", "src.main",
        "--port", str(port),
        "--identity-dir", str(identity_dir),
        "--memory-dir", str(memory_dir),
        "--data-dir", str(data_dir),
    ]
    env = os.environ.copy()
    env["USER_TIMEZONE"] = user_tz
    if model:
        env["PRIMARY_MODEL"] = model
    allowed_tools_str = normalize_tools(tools or [])
    if allowed_tools_str:
        env["ALLOWED_TOOLS"] = allowed_tools_str
    log_file = hollow_root / "data" / f"{name}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=lf,
            cwd=str(hollow_root),
            env=env,
        )
    model_str = f" [{model}]" if model else ""
    print(f"  Started {name} (pid {proc.pid}) on port {port}{model_str} — log: {log_file}")
    return proc


print("Hollow — starting agent system")
print(f"  Config: {config_path}")
print()

# Launch coordinator
coord_name = coord["name"]
coord_port = coord["port"]
coord_model = coord.get("model")
coord_identity = hollow_root / "agents" / coord_name
coord_memory = hollow_root / "agent-memory" / coord_name

if not coord_identity.exists():
    print(f"  ERROR: identity directory not found: {coord_identity}")
    print("  Run 'python setup.py' to generate agent identity files.")
    sys.exit(1)

if not coord_memory.exists():
    print(f"  ERROR: memory directory not found: {coord_memory}")
    print("  Run 'python setup.py' to generate agent memory directories.")
    sys.exit(1)

coord_data = hollow_root / "data" / coord_name
coord_data.mkdir(parents=True, exist_ok=True)
print(f"Coordinator: {coord_name}")
coord_tools = coord.get("tools", [])
p = launch(coord_name, coord_port, coord_identity, coord_memory, coord_data, model=coord_model, tools=coord_tools)
if p:
    procs.append(p)
time.sleep(1)

# Launch specialist agents
print("Agents:")
for agent in agents:
    name = agent["name"]
    port = agent["port"]
    model = agent.get("model")
    tools = agent.get("tools", [])
    identity_dir = hollow_root / "agents" / name
    memory_dir = hollow_root / "agent-memory" / name

    if not identity_dir.exists():
        print(f"  WARNING: identity directory not found for {name}: {identity_dir} — skipping")
        continue
    if not memory_dir.exists():
        print(f"  WARNING: memory directory not found for {name}: {memory_dir} — skipping")
        continue

    data_dir = hollow_root / "data" / name
    data_dir.mkdir(parents=True, exist_ok=True)
    p = launch(name, port, identity_dir, memory_dir, data_dir, model=model, tools=tools)
    if p:
        procs.append(p)

if not procs:
    print("\nNo agents launched successfully. Check configuration and run 'python setup.py'.")
    sys.exit(1)

print()
print(f"All agents launched. PIDs: {[p.pid for p in procs if p]}")
print("Logs are in ./data/<agent>.log")
print("Stop with: kill $(cat .hollow.pids)")

# Write PIDs file
pids_file = hollow_root / ".hollow.pids"
pids_file.write_text("\n".join(str(p.pid) for p in procs if p) + "\n")
print(f"PIDs written to: {pids_file}")

try:
    for p in procs:
        if p:
            p.wait()
except KeyboardInterrupt:
    print("\nShutting down...")
    for p in procs:
        if p:
            p.terminate()
PYEOF
