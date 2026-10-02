"""Checks the OpenAI side: the ChatGPT/Codex connector entry in ~/.codex/config.toml, the skill, AGENTS.md.

Uses the real Codex CLI (if it's on PATH) to confirm Codex reads the entry the way we wrote it.
Run:  python tests/test_openai.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASSED = []


def check(cond, label, detail=""):
    if not cond:
        raise AssertionError(f"FAILED: {label} {detail}")
    PASSED.append(label)
    print(f"  ok  {label}")


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="ss-openai-"))
    home = work / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    library = home / "SuperStudent"
    library.mkdir()
    os.environ.update({"HOME": str(home), "CODEX_HOME": str(codex_home), "SUPERSTUDENT_LIBRARY": str(library)})
    os.environ.pop("SUPERSTUDENT_HOME", None)
    existing = '''model = "gpt-5.5"
approval_policy = "on-request"

[profiles.study]
model_reasoning_effort = "high"

[mcp_servers.canvas-study]
command = "/Users/marc/plugins/canvas-study-connector/scripts/with-node.sh"
args = ["src/server.mjs"]

[mcp_servers.canvas-study.env]
CANVAS_DEBUG = "0"

# an older Super Student entry, as `codex mcp add` would have written it
[mcp_servers.superstudent]
command = "/old/python"
args = ["-m", "superstudent", "mcp"]

[mcp_servers.superstudent.env]
SUPERSTUDENT_HOME = "/old/home"

[projects."/Users/marc/SuperStudent"]
trust_level = "trusted"
'''
    cfg = codex_home / "config.toml"
    cfg.write_text(existing)

    sys.path.insert(0, str(ROOT))
    from superstudent.assistants import agents_skill_path, install_openai, openai_status

    print("== install into an existing Codex config")
    notes = install_openai()
    print("   " + notes.replace("\n", "\n   "))
    import tomllib

    data = tomllib.loads(cfg.read_text())
    ours = data["mcp_servers"]["superstudent"]
    check(ours["command"] == sys.executable and ours["args"] == ["-m", "superstudent", "mcp"], "connector points at this Python")
    check("env" not in ours, "stale env table from the old entry removed")
    check(ours.get("startup_timeout_sec") == 30 and ours.get("tool_timeout_sec") == 120, "timeouts set")
    check(data["mcp_servers"]["canvas-study"]["env"]["CANVAS_DEBUG"] == "0", "other connectors untouched")
    check(data["model"] == "gpt-5.5" and data["profiles"]["study"]["model_reasoning_effort"] == "high"
          and data["projects"]["/Users/marc/SuperStudent"]["trust_level"] == "trusted", "other settings untouched")
    check((codex_home / "config.toml.bak").read_text() == existing, "backup of the previous config")
    skill = agents_skill_path()
    text = skill.read_text()
    check(skill == home / ".agents/skills/super-student/SKILL.md" and text.startswith("---\nname: super-student\ndescription:"),
          "skill installed where ChatGPT/Codex look for user skills")
    check((library / "AGENTS.md").exists() and (library / ".agents/skills/super-student/SKILL.md").exists(),
          "AGENTS.md and project skill written into the library")
    check(openai_status() == "connected", "status reports connected")

    print("== running it again changes nothing")
    before = cfg.read_text()
    install_openai()
    check(cfg.read_text() == before and cfg.read_text().count("[mcp_servers.superstudent]") == 1, "idempotent, no duplicates")

    print("== a config written in a format we don't edit is left alone")
    odd = 'mcp_servers.superstudent.command = "/x"\n'
    cfg.write_text(odd)
    notes = install_openai()
    check(cfg.read_text() == odd and "by hand" in notes, "refuses to rewrite dotted-key configs", notes)

    print("== fresh machine with no Codex config yet")
    cfg.unlink()
    install_openai()
    check(tomllib.loads(cfg.read_text())["mcp_servers"]["superstudent"]["command"] == sys.executable, "creates the config")

    real_codex = shutil.which("codex", path=os.environ.get("PATH", "") + os.pathsep + "/root/.npm-global/bin")
    if real_codex:
        print("== the real Codex CLI reads it")
        cfg.write_text(existing)
        install_openai()
        env = dict(os.environ)
        got = subprocess.run([real_codex, "mcp", "get", "superstudent", "--json"], capture_output=True, text=True, env=env)
        info = json.loads(got.stdout[got.stdout.find("{"):])
        check(info["enabled"] and info["transport"]["type"] == "stdio" and info["transport"]["command"] == sys.executable
              and info["transport"]["args"] == ["-m", "superstudent", "mcp"], "codex mcp get sees our connector")
        check(info.get("startup_timeout_sec") == 30 and info.get("tool_timeout_sec") == 120, "codex reads our timeouts")
        listed = subprocess.run([real_codex, "mcp", "list"], capture_output=True, text=True, env=env).stdout
        check("superstudent" in listed and "canvas-study" in listed, "codex mcp list shows both connectors")
        cli = subprocess.run([sys.executable, "-m", "superstudent", "install-codex"], capture_output=True, text=True,
                             env=env, cwd=str(ROOT))
        check(cli.returncode == 0 and "added the 'superstudent' connector" in cli.stdout, "install-codex alias works", cli.stderr[-400:])
        app_server_check(real_codex, env, library)
    else:
        print("  (Codex CLI not on PATH; skipped the real-Codex checks)")
    print(f"\nALL {len(PASSED)} OPENAI CHECKS PASSED")


def app_server_check(codex: str, env: dict, library: Path) -> None:
    """Drive Codex's app server (the host the ChatGPT desktop app uses): start the connector, list its tools,
    and call search through it. Needs no OpenAI login."""
    import queue
    import threading
    import time

    from superstudent.index import update_index
    from superstudent.library import Library

    doc = library / "Fall 2026" / "FIN 6100 - Fixed Income" / "Notes.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntitle: Notes\ntype: page\n---\n\n# Notes\n\n## Convexity\n\nConvexity corrects duration.\n")
    update_index(Library(library))
    print("== Codex app server (what the ChatGPT desktop app talks to)")
    proc = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env, cwd=str(library))
    inbox: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [inbox.put(line) for line in proc.stdout], daemon=True).start()

    def call(rid, method, params, timeout=120):
        proc.stdin.write(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = json.loads(inbox.get(timeout=1))
            except (queue.Empty, ValueError):
                continue
            if msg.get("id") == rid:
                return msg
        return {}

    try:
        call(1, "initialize", {"clientInfo": {"name": "superstudent-test", "version": "1"}})
        proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        proc.stdin.flush()
        status = call(2, "mcpServerStatus/list", {"serverName": "superstudent"})
        servers = (status.get("result") or {}).get("data") or []
        tools = set((servers[0].get("tools") or {}).keys()) if servers else set()
        check({"search_course_materials", "read_material", "view_page", "course_file"} <= tools,
              f"Codex host starts the connector and sees its tools {sorted(tools)}")
        thread = call(3, "thread/start", {"cwd": str(library), "ephemeral": True})
        tid = ((thread.get("result") or {}).get("thread") or {}).get("id")
        res = call(4, "mcpServer/tool/call", {"server": "superstudent", "threadId": tid, "tool": "search_course_materials",
                                               "arguments": {"query": "convexity"}})
        text = " ".join(c.get("text", "") for c in (res.get("result") or {}).get("content") or [])
        check("Notes" in text and "Convexity" in text, "search works through the Codex host", text[:300])
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
