#!/usr/bin/env python3
"""Hollow setup wizard — interactive first-time and update installer."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOLLOW_ROOT = Path(__file__).parent.resolve()
CONFIG_PATH = HOLLOW_ROOT / "hollow.config.json"
CONFIG_EXAMPLE_PATH = HOLLOW_ROOT / "hollow.config.json.example"
ENV_PATH = HOLLOW_ROOT / ".env"
AGENT_MEMORY_DIR = HOLLOW_ROOT / "agent-memory"
AGENTS_DIR = HOLLOW_ROOT / "agents"
BIN_DIR = HOLLOW_ROOT / "bin"
TEMPLATES_DIR = HOLLOW_ROOT / "templates"
START_SH_PATH = HOLLOW_ROOT / "start.sh"

PORT_SCAN_START = 18792
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def cprint(text: str, color: str = "") -> None:
    codes = {
        "green": "\033[92m",
        "yellow": "\033[93m",
        "red": "\033[91m",
        "cyan": "\033[96m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "reset": "\033[0m",
    }
    if color and color in codes:
        print(f"{codes[color]}{text}{codes['reset']}")
    else:
        print(text)


def prompt(msg: str, default: str = "", secret: bool = False) -> str:
    """Prompt user for input with optional default."""
    if default:
        display = f"{msg} [{default}]: "
    else:
        display = f"{msg}: "

    if secret:
        import getpass
        val = getpass.getpass(display)
    else:
        val = input(display)

    return val.strip() or default


def confirm(msg: str, default: bool = True) -> bool:
    """Yes/no confirmation."""
    suffix = " [Y/n]" if default else " [y/N]"
    val = input(f"{msg}{suffix}: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def validate_name(name: str) -> bool:
    """Return True if name matches alphanumeric + hyphen/underscore pattern."""
    return bool(NAME_PATTERN.match(name))


def prompt_valid_name(msg: str, default: str = "") -> str:
    """Prompt until user enters a valid agent name."""
    while True:
        val = prompt(msg, default=default)
        if not val:
            cprint("Name cannot be empty.", "red")
            continue
        if not validate_name(val):
            cprint(
                f"Invalid name '{val}'. Use only letters, numbers, hyphens, and underscores.",
                "red",
            )
            continue
        return val.lower()


def is_port_available(port: int) -> bool:
    """Return True if port is not already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_available_ports(count: int, start: int = PORT_SCAN_START) -> list[int]:
    """Find `count` available ports starting from `start`."""
    ports = []
    candidate = start
    while len(ports) < count:
        if is_port_available(candidate):
            ports.append(candidate)
        else:
            cprint(f"  Port {candidate} is in use — skipping.", "dim")
        candidate += 1
    return ports


def mask_key(key: str) -> str:
    """Mask an API key for display, showing first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def render_template(template_path: Path, variables: dict[str, str]) -> str:
    """Render a template file with {{placeholder}} substitution."""
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    text = template_path.read_text()
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def generate_agent_roster_table(agents: list[dict]) -> str:
    """Generate markdown table of agents for coordinator's agents.md."""
    lines = ["| Name | Call with | Role |", "|------|-----------|------|"]
    for agent in agents:
        name = agent["name"].capitalize()
        keyword = agent["hail_keyword"]
        role = agent["role"]
        lines.append(f"| {name} | `hail {keyword}` | {role} |")
    return "\n".join(lines)


def generate_agent_routing_rules(agents: list[dict]) -> str:
    """Generate routing rules block for coordinator's agents.md."""
    lines = []
    for agent in agents:
        keyword = agent["hail_keyword"]
        role = agent["role"].lower()
        desc = agent["description"]
        # Generate a brief routing hint from description
        # Use first sentence, truncated
        hint = desc.split(".")[0].rstrip()
        lines.append(f"- {hint} → hail {keyword}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------


def load_config() -> dict | None:
    """Load hollow.config.json if it exists."""
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        cprint(f"Warning: could not parse existing config: {e}", "yellow")
        return None


def load_example_config() -> dict:
    """Load hollow.config.json.example as defaults."""
    if not CONFIG_EXAMPLE_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_EXAMPLE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def load_env() -> dict[str, str]:
    """Load existing .env key-value pairs."""
    env = {}
    if not ENV_PATH.exists():
        return env
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def write_env(env: dict[str, str]) -> None:
    """Write .env file from dict."""
    lines = ["# Hollow environment configuration", "# Generated by setup.py", ""]
    for k, v in env.items():
        lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    cprint(f"  Written: {ENV_PATH}", "green")


def write_config(config: dict) -> None:
    """Write hollow.config.json."""
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    cprint(f"  Written: {CONFIG_PATH}", "green")


# ---------------------------------------------------------------------------
# File generation helpers
# ---------------------------------------------------------------------------


def generate_coordinator_memory(config: dict) -> None:
    """Create coordinator agent-memory directory from templates."""
    coord = config["coordinator"]
    coord_name = coord["name"]
    user_name = config["user"]["name"]
    timestamp = datetime.now().strftime("%Y-%m-%d")

    memory_dir = AGENT_MEMORY_DIR / coord_name

    if memory_dir.exists():
        cprint(
            f"  Existing agent memory found for {coord_name}, skipping to preserve history.",
            "yellow",
        )
        return

    memory_dir.mkdir(parents=True, exist_ok=True)

    # Gather agent list for agents.md
    roster_table = generate_agent_roster_table(config["agents"])
    routing_rules = generate_agent_routing_rules(config["agents"])

    files = {
        "soul.md": render_template(
            TEMPLATES_DIR / "coordinator" / "soul.md",
            {"coordinator_name": coord_name.capitalize(), "user_name": user_name},
        ),
        "identity.md": render_template(
            TEMPLATES_DIR / "coordinator" / "identity.md",
            {"coordinator_name": coord_name.capitalize(), "user_name": user_name},
        ),
        "agents.md": render_template(
            TEMPLATES_DIR / "coordinator" / "agents.md",
            {
                "coordinator_name": coord_name.capitalize(),
                "agent_roster_table": roster_table,
                "agent_routing_rules": routing_rules,
            },
        ),
        "memory.md": render_template(
            TEMPLATES_DIR / "coordinator" / "memory.md",
            {
                "coordinator_name": coord_name.capitalize(),
                "init_timestamp": timestamp,
            },
        ),
    }

    for filename, content in files.items():
        (memory_dir / filename).write_text(content)

    # Create sessions and memory subdirs
    (memory_dir / "sessions").mkdir(exist_ok=True)
    (memory_dir / "memory").mkdir(exist_ok=True)

    cprint(f"  Created coordinator memory: {memory_dir}", "green")


def generate_agent_memory(agent: dict, config: dict) -> None:
    """Create a specialist agent's agent-memory directory from templates."""
    agent_name = agent["name"]
    coord_name = config["coordinator"]["name"]
    timestamp = datetime.now().strftime("%Y-%m-%d")

    memory_dir = AGENT_MEMORY_DIR / agent_name

    if memory_dir.exists():
        cprint(
            f"  Existing agent memory found for {agent_name}, skipping to preserve history.",
            "yellow",
        )
        return

    memory_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "memory.md": render_template(
            TEMPLATES_DIR / "agent" / "memory.md",
            {
                "agent_name": agent_name.capitalize(),
                "agent_role": agent["role"],
                "init_timestamp": timestamp,
            },
        ),
    }

    for filename, content in files.items():
        (memory_dir / filename).write_text(content)

    # agents.md for specialist (knows about the team)
    roster_table = generate_agent_roster_table(config["agents"])
    agents_md = f"# Team — Hollow Agent System\n\nYou are part of a team of specialist agents coordinated by {coord_name.capitalize()}.\n\n## Team Roster\n| Name | Port | Role |\n|------|------|------|\n"
    agents_md += f"| {coord_name.capitalize()} | {config['coordinator']['port']} | Coordinator — {config['user']['name']}'s primary interface |\n"
    for a in config["agents"]:
        agents_md += f"| {a['name'].capitalize()} | {a['port']} | {a['role']} |\n"
    agents_md += "\n## Your Role\nYou receive tasks from the coordinator via HTTP POST. You do your specialized job and return results to the coordinator.\n\n## Tools\nBash, Read, Write, Edit, Glob, Grep — all available.\nWebSearch and WebFetch available for research.\n"
    (memory_dir / "agents.md").write_text(agents_md)

    # Sessions and memory subdirs
    (memory_dir / "sessions").mkdir(exist_ok=True)
    (memory_dir / "memory").mkdir(exist_ok=True)

    cprint(f"  Created agent memory: {memory_dir}", "green")


def generate_agent_identity_files(agent: dict, config: dict) -> None:
    """Create a specialist agent's agents/ identity directory from templates."""
    agent_name = agent["name"]
    coord_name = config["coordinator"]["name"]

    identity_dir = AGENTS_DIR / agent_name

    if identity_dir.exists():
        cprint(
            f"  Existing identity files found for {agent_name} in agents/, skipping to preserve customizations.",
            "yellow",
        )
        return

    identity_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "soul.md": render_template(
            TEMPLATES_DIR / "agent" / "soul.md",
            {
                "agent_name": agent_name.capitalize(),
                "agent_role": agent["role"],
                "agent_description": agent["description"],
                "coordinator_name": coord_name.capitalize(),
            },
        ),
        "identity.md": render_template(
            TEMPLATES_DIR / "agent" / "identity.md",
            {
                "agent_name": agent_name.capitalize(),
                "agent_role": agent["role"],
                "agent_description": agent["description"],
                "coordinator_name": coord_name.capitalize(),
            },
        ),
    }

    for filename, content in files.items():
        (identity_dir / filename).write_text(content)

    cprint(f"  Created agent identity files: {identity_dir}", "green")


def generate_coordinator_identity_files(config: dict) -> None:
    """Create coordinator's agents/ identity directory from templates."""
    coord = config["coordinator"]
    coord_name = coord["name"]
    user_name = config["user"]["name"]

    identity_dir = AGENTS_DIR / coord_name

    if identity_dir.exists():
        cprint(
            f"  Existing identity files found for {coord_name} in agents/, skipping.",
            "yellow",
        )
        return

    identity_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "soul.md": render_template(
            TEMPLATES_DIR / "coordinator" / "soul.md",
            {"coordinator_name": coord_name.capitalize(), "user_name": user_name},
        ),
        "identity.md": render_template(
            TEMPLATES_DIR / "coordinator" / "identity.md",
            {"coordinator_name": coord_name.capitalize(), "user_name": user_name},
        ),
    }

    for filename, content in files.items():
        (identity_dir / filename).write_text(content)

    cprint(f"  Created coordinator identity files: {identity_dir}", "green")


def generate_hail(config: dict) -> None:
    """Write bin/hail as a dynamic script that reads hollow.config.json at runtime."""
    hail_path = BIN_DIR / "hail"
    content = '''#!/usr/bin/env python3
"""Route tasks to specialist agents via HTTP POST.

Reads agent list and ports from hollow.config.json at runtime.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOLLOW_ROOT = Path(__file__).parent.parent.resolve()
CONFIG_PATH = HOLLOW_ROOT / "hollow.config.json"
TIMEOUT = 300


def load_agents() -> dict[str, str]:
    """Load agent name -> URL mapping from hollow.config.json."""
    if not CONFIG_PATH.exists():
        print(
            f"Error: hollow.config.json not found at {CONFIG_PATH}\\n"
            "Run 'python setup.py' to generate it.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error: could not parse hollow.config.json: {e}", file=sys.stderr)
        sys.exit(1)

    agents = {}
    for agent in cfg.get("agents", []):
        keyword = agent.get("hail_keyword", agent.get("name", ""))
        port = agent.get("port")
        if keyword and port:
            agents[keyword.lower()] = f"http://127.0.0.1:{port}/ask"

    return agents


def main():
    agents = load_agents()

    if len(sys.argv) < 3:
        print(f"Usage: hail <agent> <task>")
        print(f"Agents: {', '.join(sorted(agents))}")
        sys.exit(1)

    agent = sys.argv[1].lower()
    task = " ".join(sys.argv[2:])

    if agent not in agents:
        print(f"Unknown agent: {agent}")
        print(f"Available: {', '.join(sorted(agents))}")
        sys.exit(1)

    url = agents[agent]
    chat_id = f"{agent}_main"
    payload = json.dumps({"message": task, "chat_id": chat_id}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            print(data.get("response", json.dumps(data, indent=2)))
    except urllib.error.URLError as e:
        print(f"Error connecting to {agent} at {url}: {e}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError:
        print(f"Timeout after {TIMEOUT}s waiting for {agent}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
'''
    hail_path.write_text(content)
    hail_path.chmod(0o755)
    cprint(f"  Written: {hail_path}", "green")


def generate_start_sh(config: dict) -> None:
    """Generate start.sh that reads hollow.config.json via Python (no jq)."""
    content = '''#!/usr/bin/env bash
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
python3 - "$CONFIG" "$HOLLOW_ROOT" <<\'PYEOF\'
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

def launch(name, port, identity_dir, memory_dir):
    if port_in_use(port):
        print(f"  WARNING: port {port} already in use — {name} may already be running")
        return None
    cmd = [
        "uv", "run", "python", "-m", "src.main",
        "--port", str(port),
        "--identity-dir", str(identity_dir),
        "--memory-dir", str(memory_dir),
    ]
    env = os.environ.copy()
    env["USER_TIMEZONE"] = user_tz
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
    print(f"  Started {name} (pid {proc.pid}) on port {port} — log: {log_file}")
    return proc

print("Hollow — starting agent system")
print(f"  Config: {config_path}")
print()

# Launch coordinator
coord_name = coord["name"]
coord_port = coord["port"]
coord_identity = hollow_root / "agents" / coord_name
coord_memory = hollow_root / "agent-memory" / coord_name

print(f"Coordinator: {coord_name}")
p = launch(coord_name, coord_port, coord_identity, coord_memory)
if p:
    procs.append(p)
time.sleep(1)

# Launch specialist agents
print(f"Agents:")
for agent in agents:
    name = agent["name"]
    port = agent["port"]
    identity_dir = hollow_root / "agents" / name
    memory_dir = hollow_root / "agent-memory" / name
    p = launch(name, port, identity_dir, memory_dir)
    if p:
        procs.append(p)

print()
print(f"All agents launched. PIDs: {[p.pid for p in procs if p]}")
print("Logs are in ./data/<agent>.log")
print("Stop with: kill $(cat .hollow.pids) or Ctrl+C if running in foreground")

# Write PIDs file
pids_file = hollow_root / ".hollow.pids"
pids_file.write_text("\\n".join(str(p.pid) for p in procs if p) + "\\n")

try:
    for p in procs:
        if p:
            p.wait()
except KeyboardInterrupt:
    print("\\nShutting down...")
    for p in procs:
        if p:
            p.terminate()
PYEOF
'''
    START_SH_PATH.write_text(content)
    START_SH_PATH.chmod(0o755)
    cprint(f"  Written: {START_SH_PATH}", "green")


# ---------------------------------------------------------------------------
# Setup wizard sections
# ---------------------------------------------------------------------------


def section_header(title: str) -> None:
    print()
    cprint(f"{'=' * 60}", "cyan")
    cprint(f"  {title}", "cyan")
    cprint(f"{'=' * 60}", "cyan")
    print()


def collect_user_info(existing: dict | None) -> dict:
    """Collect user name and timezone."""
    section_header("User Configuration")
    defaults = existing.get("user", {}) if existing else {}
    example = load_example_config().get("user", {})

    name = prompt(
        "Your name",
        default=defaults.get("name") or example.get("name", ""),
    )
    tz = prompt(
        "Your timezone (e.g. America/Chicago)",
        default=defaults.get("timezone") or example.get("timezone", "America/Chicago"),
    )
    return {"name": name, "timezone": tz}


def collect_coordinator_info(existing: dict | None, agent_count: int) -> dict:
    """Collect coordinator name and port."""
    section_header("Coordinator Configuration")
    defaults = existing.get("coordinator", {}) if existing else {}
    example = load_example_config().get("coordinator", {})

    cprint(
        "The coordinator is the primary agent that routes tasks to the specialist team.",
        "dim",
    )
    name = prompt_valid_name(
        "Coordinator name",
        default=defaults.get("name") or example.get("name", "coordinator"),
    )
    description = prompt(
        "Coordinator description",
        default=defaults.get("description") or example.get("description", "Coordinator — primary user interface"),
    )

    # Scan for available port
    cprint("\nScanning for available ports...", "dim")
    existing_port = defaults.get("port")
    if existing_port and is_port_available(existing_port):
        port = existing_port
        cprint(f"  Using existing port {port} (available)", "dim")
    else:
        if existing_port and not is_port_available(existing_port):
            cprint(f"  Port {existing_port} is in use — finding new port...", "yellow")
        # Reserve ports for coordinator + agents
        needed = 1 + agent_count
        all_ports = find_available_ports(needed)
        port = all_ports[0]
        cprint(f"  Assigned coordinator port: {port}", "dim")

    return {"name": name, "port": port, "description": description}


def collect_agents_info(existing: dict | None, coord_port: int) -> list[dict]:
    """Collect info for each specialist agent."""
    section_header("Specialist Agents")
    existing_agents = {a["name"]: a for a in (existing.get("agents", []) if existing else [])}
    example_agents = {a["name"]: a for a in load_example_config().get("agents", [])}

    n_str = prompt("Number of specialist agents", default="5")
    try:
        n = int(n_str)
    except ValueError:
        n = 5
    n = max(1, n)

    # Find ports for all agents (scan excluding coord port)
    cprint("\nScanning for available agent ports...", "dim")
    needed_ports = n
    available = []
    candidate = PORT_SCAN_START
    while len(available) < needed_ports:
        if candidate != coord_port and is_port_available(candidate):
            available.append(candidate)
        elif candidate == coord_port:
            cprint(f"  Skipping {candidate} (used by coordinator)", "dim")
        else:
            cprint(f"  Port {candidate} in use — skipping", "dim")
        candidate += 1

    # Populate defaults from existing or example, fill rest with blanks
    example_list = list(example_agents.values())

    agents = []
    cprint(f"\nConfiguring {n} specialist agents:\n", "")
    for i in range(n):
        ex_name = example_list[i]["name"] if i < len(example_list) else ""
        ex = existing_agents.get(ex_name) or (example_list[i] if i < len(example_list) else {})

        cprint(f"--- Agent {i + 1} ---", "bold")
        name = prompt_valid_name(
            "  Name",
            default=ex.get("name", f"agent{i+1}"),
        )

        # If this exact name existed before with an available port, reuse it
        ex_port = existing_agents.get(name, {}).get("port")
        if ex_port and is_port_available(ex_port) and ex_port not in [a["port"] for a in agents] and ex_port != coord_port:
            port = ex_port
        else:
            port = available[i]

        role = prompt(
            "  Role (short phrase)",
            default=ex.get("role", ""),
        )
        description = prompt(
            "  Description (what they do)",
            default=ex.get("description", ""),
        )
        hail_keyword = prompt(
            "  Hail keyword (used in: hail <keyword> \"task\")",
            default=ex.get("hail_keyword", name),
        )

        agents.append(
            {
                "name": name,
                "port": port,
                "role": role,
                "description": description,
                "hail_keyword": hail_keyword.lower(),
            }
        )

    return agents


def collect_api_keys() -> dict[str, str]:
    """Collect API keys, respecting existing .env values."""
    section_header("API Keys & Credentials")
    existing_env = load_env()

    env = dict(existing_env)

    def handle_key(env_key: str, label: str, required: bool = False) -> None:
        current = existing_env.get(env_key, "")
        if current:
            cprint(f"  {label}: {mask_key(current)} (existing)", "dim")
            if not confirm(f"  Keep existing {label}?", default=True):
                new_val = prompt(f"  Enter new {label}", secret=True)
                if new_val:
                    env[env_key] = new_val
        else:
            if required:
                new_val = prompt(f"  {label} (required)", secret=True)
            else:
                new_val = prompt(f"  {label} (optional, press Enter to skip)", secret=True)
            if new_val:
                env[env_key] = new_val
            elif not required:
                cprint(f"  Skipping {label}", "dim")

    handle_key("ANTHROPIC_API_KEY", "Anthropic API Key")
    handle_key("XAI_API_KEY", "xAI API Key (optional — for Grok/X search)")
    handle_key("TELEGRAM_BOT_TOKEN", "Telegram Bot Token (optional)")

    # Telegram allowed users
    current_users = existing_env.get("TELEGRAM_ALLOWED_USERS", "")
    if current_users:
        cprint(f"  Telegram allowed users: {current_users} (existing)", "dim")
        if not confirm("  Keep existing Telegram allowed users?", default=True):
            new_val = prompt("  Telegram allowed user IDs (comma-separated)", default="")
            env["TELEGRAM_ALLOWED_USERS"] = new_val
    else:
        new_val = prompt(
            "  Telegram allowed user IDs (comma-separated, optional)", default=""
        )
        if new_val:
            env["TELEGRAM_ALLOWED_USERS"] = new_val

    return env


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------


def run_wizard() -> None:
    cprint("\nHollow Setup Wizard", "bold")
    cprint("=" * 60, "cyan")

    existing_config = load_config()

    if existing_config:
        cprint("\nExisting hollow.config.json detected.", "yellow")
        choice = prompt(
            "Update existing config or start fresh? [update/fresh]",
            default="update",
        )
        if choice.strip().lower() == "fresh":
            if not confirm(
                "Are you sure? Starting fresh will generate new config (existing agent memory is NEVER overwritten)",
                default=False,
            ):
                cprint("Aborted.", "red")
                sys.exit(0)
            existing_config = None
        else:
            cprint("Updating existing configuration...", "dim")
    else:
        cprint("\nNo hollow.config.json found — running first-time setup.\n", "dim")

    # 1. User info
    user_info = collect_user_info(existing_config)

    # 2. Agent count (needed before coordinator port scan)
    section_header("Team Size")
    n_agents_str = prompt("How many specialist agents?", default="5")
    try:
        n_agents_preview = int(n_agents_str)
    except ValueError:
        n_agents_preview = 5

    # 3. Coordinator
    coord_info = collect_coordinator_info(existing_config, n_agents_preview)

    # 4. Agents
    agents_info = collect_agents_info(existing_config, coord_info["port"])

    # 5. API keys / .env
    env_data = collect_api_keys()

    # Build config
    config = {
        "version": "v1",
        "coordinator": coord_info,
        "agents": agents_info,
        "user": user_info,
    }

    # Preview
    section_header("Review Configuration")
    cprint(json.dumps(config, indent=2), "")
    print()
    if not confirm("Write this configuration?", default=True):
        cprint("Aborted — nothing written.", "red")
        sys.exit(0)

    # Write files
    section_header("Writing Files")

    # hollow.config.json
    write_config(config)

    # .env
    if ENV_PATH.exists():
        if confirm(
            f"  .env already exists. Overwrite with updated keys?", default=True
        ):
            write_env(env_data)
        else:
            cprint("  Skipping .env (keeping existing)", "yellow")
    else:
        write_env(env_data)

    # Templates -> agent memory (idempotent — skip if exists)
    section_header("Generating Agent Memory")
    cprint("Note: Existing agent memory directories are never overwritten.", "dim")
    generate_coordinator_memory(config)
    for agent in agents_info:
        generate_agent_memory(agent, config)

    # Templates -> agents/ identity dirs (idempotent)
    section_header("Generating Agent Identity Files")
    cprint("Note: Existing identity file directories are never overwritten.", "dim")
    generate_coordinator_identity_files(config)
    for agent in agents_info:
        generate_agent_identity_files(agent, config)

    # bin/hail (dynamic)
    section_header("Generating bin/hail")
    generate_hail(config)

    # start.sh
    section_header("Generating start.sh")
    generate_start_sh(config)

    # Done
    section_header("Setup Complete")
    cprint("Hollow is configured. Next steps:", "green")
    print()
    print("  1. Review hollow.config.json")
    print("  2. Check .env (API keys)")
    print(f"  3. Start the system: ./start.sh")
    print()
    cprint("Agent memory directories (live data — never touch):", "dim")
    print(f"  {AGENT_MEMORY_DIR}")
    print()
    cprint("To re-run setup at any time: python setup.py", "dim")
    print()


if __name__ == "__main__":
    run_wizard()
