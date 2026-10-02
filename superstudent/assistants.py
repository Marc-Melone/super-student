"""Connect the library to AI assistants.

Claude:  Claude desktop app (claude_desktop_config.json) and Claude Code (skill + `claude mcp add`).
OpenAI:  ChatGPT desktop app (Work, Chat and Codex) and the Codex CLI, which share ~/.codex/config.toml,
         plus the user-level skill in ~/.agents/skills and AGENTS.md inside the library folder.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from .guides import write_skill
from .util import atomic_write_text

SERVER_NAME = "superstudent"


def server_command() -> Tuple[str, List[str]]:
    return sys.executable, ["-m", "superstudent", "mcp"]


def _server_env() -> Dict[str, str]:
    return {"SUPERSTUDENT_HOME": os.environ["SUPERSTUDENT_HOME"]} if os.environ.get("SUPERSTUDENT_HOME") else {}


def _write_library_guides() -> str:
    from .config import library_path, load_config
    from .guides import install_library_guides
    from .library import Library

    lib = Library(library_path(load_config()))
    if lib.root.exists():
        install_library_guides(lib)
        return str(lib.root)
    return ""


# ---------------------------------------------------------------- Claude

def claude_desktop_config() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def install_claude(desktop: bool = True, code: bool = True) -> str:
    notes = []
    command, args = server_command()
    server: Dict[str, object] = {"command": command, "args": args}
    if _server_env():
        server["env"] = _server_env()
    if desktop:
        path = claude_desktop_config()
        data: Dict[str, object] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except ValueError:
                return f"Couldn't read {path} (invalid JSON), so it wasn't changed."
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        servers = data.setdefault("mcpServers", {})
        if isinstance(servers, dict):
            servers[SERVER_NAME] = server
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        notes.append(f"Claude desktop app: added the '{SERVER_NAME}' connector to {path}. Quit and reopen Claude to load it.")
    if code:
        skill = Path.home() / ".claude" / "skills" / "super-student" / "SKILL.md"
        write_skill(skill.parent)
        notes.append(f"Claude Code: installed the super-student skill at {skill}.")
        claude = shutil.which("claude")
        if claude:
            subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True)
            res = subprocess.run([claude, "mcp", "add", "--scope", "user", SERVER_NAME, "--", command, *args],
                                 capture_output=True, text=True)
            notes.append("Claude Code: connector added." if res.returncode == 0 else
                         f"Claude Code: couldn't add the connector automatically ({(res.stderr or res.stdout).strip()[:200]}).")
        else:
            notes.append(f"Claude Code (if you use it): claude mcp add --scope user {SERVER_NAME} -- {command} {' '.join(args)}")
    where = _write_library_guides()
    if where:
        notes.append(f"Library instructions for Claude: {where}/CLAUDE.md")
    return "\n".join(notes)


def claude_status() -> str:
    registered = False
    try:
        registered = SERVER_NAME in (json.loads(claude_desktop_config().read_text()).get("mcpServers") or {})
    except (OSError, ValueError, AttributeError):
        pass
    skill = (Path.home() / ".claude" / "skills" / "super-student" / "SKILL.md").exists()
    if registered or skill:
        return "connected" + ("" if registered else " (Claude Code only)")
    return "not connected (run: superstudent install-claude)"


# ---------------------------------------------------------------- OpenAI (ChatGPT desktop app + Codex)

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def agents_skill_path() -> Path:
    return Path.home() / ".agents" / "skills" / "super-student" / "SKILL.md"


_HEADER = re.compile(r"^\s*\[\[?\s*(.+?)\s*\]\]?\s*(?:#.*)?$")


def _split_dotted(name: str) -> List[str]:
    parts, buf, quote = [], "", ""
    for ch in name:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    parts.append(buf.strip())
    return parts


def _is_ours(table: str) -> bool:
    parts = _split_dotted(table)
    return len(parts) >= 2 and parts[0] == "mcp_servers" and parts[1] == SERVER_NAME


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def codex_config_block() -> str:
    command, args = server_command()
    lines = [f"[mcp_servers.{SERVER_NAME}]",
             f"command = {_toml_str(command)}",
             "args = [" + ", ".join(_toml_str(a) for a in args) + "]",
             "startup_timeout_sec = 30",
             "tool_timeout_sec = 120"]
    env = _server_env()
    if env:
        lines += ["", f"[mcp_servers.{SERVER_NAME}.env]"] + [f"{k} = {_toml_str(v)}" for k, v in env.items()]
    return "\n".join(lines) + "\n"


def update_codex_config(text: str) -> str:
    """Replace (or add) our [mcp_servers.superstudent] tables, leaving everything else untouched."""
    kept: List[str] = []
    skipping = False
    for line in text.splitlines():
        match = _HEADER.match(line)
        if match and not line.lstrip().startswith("#"):
            skipping = _is_ours(match.group(1))
        if not skipping:
            kept.append(line)
    body = "\n".join(kept).rstrip()
    return (body + "\n\n" if body else "") + codex_config_block()


def _check_toml(text: str) -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: nothing to validate with
        return
    data = tomllib.loads(text)
    server = (data.get("mcp_servers") or {}).get(SERVER_NAME) or {}
    if not server.get("command"):
        raise ValueError("the connector entry didn't come out right")


def install_openai() -> str:
    notes = []
    command, args = server_command()
    cfg_path = codex_home() / "config.toml"
    original = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    manual = (f"Add it by hand in the ChatGPT desktop app: Settings > MCP servers > Add server > STDIO, "
              f"name '{SERVER_NAME}', command: {command} {' '.join(args)}")
    if re.search(rf"^\s*mcp_servers\s*=|^\s*mcp_servers\.{SERVER_NAME}\.", original, re.M):
        notes.append(f"Your {cfg_path} defines connectors in a format I won't edit automatically. {manual}")
    else:
        updated = update_codex_config(original)
        try:
            _check_toml(updated)
        except ValueError as exc:
            notes.append(f"Couldn't update {cfg_path} safely ({exc}), so it wasn't changed. {manual}")
        else:
            if cfg_path.exists():
                shutil.copy2(cfg_path, cfg_path.with_name("config.toml.bak"))
            atomic_write_text(cfg_path, updated)
            notes.append(f"ChatGPT desktop app and Codex: added the '{SERVER_NAME}' connector to {cfg_path}"
                         + (" (previous version saved as config.toml.bak)." if original else "."))
    skill = agents_skill_path()
    write_skill(skill.parent)
    notes.append(f"Installed the super-student skill at {skill}.")
    where = _write_library_guides()
    if where:
        notes.append(f"Library instructions for ChatGPT and Codex: {where}/AGENTS.md")
    notes.append("Next, in the ChatGPT desktop app: quit and reopen it, then create a project, choose Edit project > "
                 "Add folder, pick your SuperStudent folder and make it the primary folder. Study in that project "
                 "with Work. Type /mcp in the message box to confirm 'superstudent' is connected.")
    return "\n".join(notes)


def openai_status() -> str:
    cfg_path = codex_home() / "config.toml"
    registered = False
    try:
        text = cfg_path.read_text(encoding="utf-8")
        try:
            import tomllib

            registered = SERVER_NAME in (tomllib.loads(text).get("mcp_servers") or {})
        except ModuleNotFoundError:
            registered = bool(re.search(rf"^\s*\[\s*mcp_servers\.{SERVER_NAME}\s*\]", text, re.M))
    except (OSError, ValueError):
        pass
    skill = agents_skill_path().exists()
    if registered and skill:
        return "connected"
    if registered or skill:
        return "partly connected (run: superstudent install-openai)"
    return "not connected (run: superstudent install-openai)"


# ---------------------------------------------------------------- removal (uninstall)

def _strip_our_tables(text: str) -> str:
    kept: List[str] = []
    skipping = False
    for line in text.splitlines():
        match = _HEADER.match(line)
        if match and not line.lstrip().startswith("#"):
            skipping = _is_ours(match.group(1))
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip() + "\n"


def remove_openai() -> str:
    cfg_path = codex_home() / "config.toml"
    notes = []
    try:
        original = cfg_path.read_text(encoding="utf-8")
    except OSError:
        original = ""
    if original:
        updated = _strip_our_tables(original)
        if updated.strip() != original.strip():
            shutil.copy2(cfg_path, cfg_path.with_name("config.toml.bak"))
            atomic_write_text(cfg_path, updated)
            notes.append(f"Removed the '{SERVER_NAME}' connector from {cfg_path}.")
    shutil.rmtree(agents_skill_path().parent, ignore_errors=True)
    notes.append("Removed the super-student skill for ChatGPT and Codex.")
    return "\n".join(notes)


def remove_claude() -> str:
    notes = []
    path = claude_desktop_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and SERVER_NAME in (data.get("mcpServers") or {}):
        shutil.copy2(path, path.with_name(path.name + ".bak"))
        data["mcpServers"].pop(SERVER_NAME, None)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        notes.append(f"Removed the '{SERVER_NAME}' connector from {path}.")
    shutil.rmtree(Path.home() / ".claude" / "skills" / "super-student", ignore_errors=True)
    claude = shutil.which("claude")
    if claude:
        subprocess.run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], capture_output=True)
    notes.append("Removed the super-student skill for Claude.")
    return "\n".join(notes)
