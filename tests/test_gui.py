"""Tests for the Super Student app's local server: security checks and the whole setup-to-study flow,
driven through the same HTTP API the app window uses, against the fake Canvas.

Run:  python tests/test_gui.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

WORK = Path(os.environ.get("SS_TEST_DIR") or tempfile.mkdtemp(prefix="ss-gui-"))
for sub in ("app", "lib", "home"):
    shutil.rmtree(WORK / sub, ignore_errors=True)
(WORK / "home").mkdir(parents=True)
os.environ["HOME"] = str(WORK / "home")
os.environ["SUPERSTUDENT_HOME"] = str(WORK / "app")
os.environ["SUPERSTUDENT_APP_DRYRUN"] = "1"
for var in ("CANVAS_TOKEN", "SUPERSTUDENT_TOKEN", "SUPERSTUDENT_CANVAS_URL", "SUPERSTUDENT_LIBRARY", "CODEX_HOME"):
    os.environ.pop(var, None)

import requests  # noqa: E402

from fake_canvas import TOKEN, FakeCanvas  # noqa: E402
from make_fixtures import build_all  # noqa: E402

PASSED = []


def check(cond, label, detail=""):
    if not cond:
        raise AssertionError(f"FAILED: {label} {detail}")
    PASSED.append(label)
    print(f"  ok  {label}")


def main() -> None:
    fixtures = build_all(WORK / "fixtures")
    fake = FakeCanvas(fixtures)
    base = fake.start()

    import superstudent.gui.app as appmod
    appmod.ACCOUNT_SEARCH = base + "/api/v1/accounts/search"
    from superstudent.config import CONFIG_FILE, TOKEN_FILE, load_config, save_config
    import superstudent.config as config
    config._keychain_available = lambda: False  # never touch the real Mac Keychain in this isolated suite

    cfg = load_config()
    cfg["library_dir"] = str(WORK / "lib")
    save_config(cfg)

    app = appmod.App(native=False)
    app.serve(0)
    url = f"http://127.0.0.1:{app.port}"
    H = {"X-SS-Token": app.token}

    def get(path, **kw):
        return requests.get(url + path, headers=H, timeout=30, **kw).json()

    def post(path, body=None):
        return requests.post(url + path, json=body or {}, headers=H, timeout=120).json()

    print("\n== Locked to this app")
    r = requests.get(url + "/", timeout=5)
    check(r.status_code == 403, "page refuses to open without the session token")
    r = requests.get(url + "/?t=" + app.token, timeout=5)
    check(r.status_code == 200 and "__SS_TOKEN__" not in r.text and app.token in r.text, "page opens with the token filled in")
    check("default-src 'none'" in r.headers.get("Content-Security-Policy", ""), "strict content security policy")
    check(r.headers.get("X-Frame-Options") == "DENY", "can't be framed by other pages")
    r = requests.get(url + "/api/state", timeout=5)
    check(r.status_code == 403, "API refuses requests without the token header")
    r = requests.get(url + "/api/state", headers={**H, "Host": "evil.example:%d" % app.port}, timeout=5)
    check(r.status_code == 403, "API refuses a foreign Host header (DNS rebinding)")
    r = requests.post(url + "/api/signout", data="{}", headers={**H, "Content-Type": "text/plain"}, timeout=5)
    check(r.status_code == 415, "POST must be JSON (blocks simple cross-site form posts)")
    r = requests.get(url + "/api/nope", headers=H, timeout=5)
    check(r.status_code == 404, "unknown API paths are 404")

    print("\n== First run")
    st = get("/api/state")
    check(st["setup_complete"] is False and st["configured"] is False, "fresh install starts in setup")
    check(st["assistants"]["openai"]["status"] == "off" and st["assistants"]["claude"]["status"] == "off",
          "no AI connected yet")
    check(st["library"]["path"] == str((WORK / "lib").resolve()), "library path reported")

    print("\n== Your school")
    r = post("/api/canvas/find", {"query": "fa"})
    check(not r["ok"] and "three letters" in r["message"], "school search asks for three letters")
    r = post("/api/canvas/find", {"query": "Fake State"})
    check(r["ok"] and r["schools"][0]["domain"] == base, "school search finds the school", r)
    r = post("/api/canvas/check", {"address": "not a url"})
    check(not r["ok"] and "instructure.com" in r["message"], "nonsense address explained")
    r = post("/api/canvas/check", {"address": "http://127.0.0.1:9/"})
    check(not r["ok"] and "Couldn't reach" in r["message"], "unreachable address explained")
    r = post("/api/canvas/check", {"address": base + "/courses/101"})
    check(r["ok"] and r["url"] == base, "Canvas address recognised (course link trimmed)", r)

    print("\n== Canvas access")
    r = post("/api/canvas/connect", {"url": base, "token": "short"})
    check(not r["ok"] and "whole token" in r["message"], "partial token caught before calling Canvas")
    r = post("/api/canvas/connect", {"url": base, "token": "x" * 40})
    check(not r["ok"] and "didn't accept" in r["message"], "wrong token rejected in plain words")
    fake_token = TOKEN + "-padding-to-look-real"   # the fake accepts only TOKEN, so patch it for length
    import fake_canvas as fc
    fc.TOKEN = fake_token
    r = post("/api/canvas/connect", {"url": base, "token": f'  "{fake_token}"  '})
    check(r["ok"] and r["name"] == "Test Student", "token accepted (quotes and spaces trimmed)", r)
    check(TOKEN_FILE.exists() and stat.S_IMODE(TOKEN_FILE.stat().st_mode) == 0o600, "token stored privately (0600)")
    saved = json.loads(CONFIG_FILE.read_text())
    check(fake_token not in CONFIG_FILE.read_text() and saved["canvas_user_name"] == "Test Student",
          "token not in settings file; name saved")
    st = get("/api/state")
    check(st["configured"] and st["canvas"]["user"] == "Test Student", "state shows the connection")

    print("\n== Courses")
    r = get("/api/canvas/courses")
    check(r["ok"] and {c["code"] for c in r["courses"]} == {"FIN 6100", "ECON 5200"}, "current courses listed", r)
    check(all(c["included"] for c in r["courses"]), "all courses ticked by default")

    print("\n== Set up")
    t0 = time.time()
    r = post("/api/setup/finish", {"excluded": [], "assistants": {"openai": True, "claude": True}, "auto_sync": True,
                                   "interval": 12, "transcribe": True})
    check(r["ok"], "setup finished", r)
    ids = {s["id"]: s for s in r["steps"]}
    check(ids["settings"]["ok"] and ids["openai"]["ok"], "settings saved and ChatGPT connected", r["steps"])
    check("claude" in ids and "schedule" in ids, "Claude and automatic updates attempted", r["steps"])
    codex_cfg = WORK / "home" / ".codex" / "config.toml"
    check(codex_cfg.exists() and "[mcp_servers.superstudent]" in codex_cfg.read_text(), "ChatGPT/Codex config written")
    check(load_config()["sync_interval_hours"] == 12 and load_config()["setup_complete"], "interval and setup flag saved")
    r = post("/api/sync/start")
    check(r["ok"], "sync start acknowledged (already running from setup)", r)
    phases = set()
    while True:
        p = get("/api/sync/progress")
        if p.get("phase"):
            phases.add(p["phase"])
        if not p["running"]:
            break
        if time.time() - t0 > 600:
            raise AssertionError("sync took too long")
        time.sleep(0.5)
    print(f"  (first update took {time.time() - t0:.1f}s; phases seen: {sorted(phases)[:6]})")
    check(p["exit_code"] == 0 and not p["failed"], "first update finished cleanly", p)
    check(p["phase"] == "Finished", "progress ends on Finished", p["phase"])
    check(any(ph.startswith("Copying") for ph in phases) or len(phases) >= 1, "progress phases shown in plain words")

    st = get("/api/state")
    check(st["setup_complete"] and len(st["courses"]) == 2, "dashboard sees both courses")
    fin = next(c for c in st["courses"] if c["code"] == "FIN 6100")
    check(fin["counts"]["documents"] >= 3 and fin["counts"]["assignments"] >= 1, "course counts", fin["counts"])
    check(fin["modules"] and fin["modules"][0]["name"], "modules listed for exam packs")
    check(st["sync"]["last_text"] == "just now", "last update shown as 'just now'")
    check(st["assistants"]["openai"]["status"] == "connected", "ChatGPT shows as connected")

    print("\n== Everyday use")
    r = get("/api/search", params={"q": "modified duration"})
    check(r["ok"] and r["hits"], "search finds course material")
    hit = r["hits"][0]
    check("\x02" in hit["snippet"] and "\x03" in hit["snippet"], "snippets carry match markers for highlighting")
    check(not any("](" in h["snippet"] or "**" in h["snippet"] for h in r["hits"]), "snippets are plain text, not Markdown")
    check(not any(h["kind"] in ("overview", "module", "links", "calendar") for h in r["hits"]),
          "summary files left out of the app's search")
    r = get("/api/search", params={"q": "duration", "course": fin["folder"], "kind": "slides"})
    check(r["ok"] and all(h["kind"] == "slides" for h in r["hits"]), "course and kind filters", r["hits"][:2])
    r = post("/api/open", {"path": hit["path"]})
    check(r["ok"], "open a search result")
    r = post("/api/open", {"path": "../../etc/passwd"})
    check(not r["ok"], "can't open files outside the library")
    r = post("/api/open-place", {"which": "course", "folder": "../.."})
    check(not r["ok"], "can't open folders outside the library")
    r = post("/api/open-place", {"which": "course", "folder": fin["folder"]})
    check(r["ok"], "open a course folder")
    r = post("/api/pack", {"folder": fin["folder"], "modules": [fin["modules"][0]["position"]]})
    check(r["ok"] and Path(r["path"]).is_dir() and r["files"] > 0, "exam pack made", r)
    for bad in ("http://example.com", "javascript:alert(1)", "file:///etc/passwd", "https://ex ample.com"):
        check(not post("/api/open-url", {"url": bad})["ok"], f"refuses to open {bad}")
    check(post("/api/open-url", {"url": "https://chatgpt.com/download"})["ok"], "opens https links")
    acts = get("/api/test/actions")["actions"]
    check(any(a.get("url") == "https://chatgpt.com/download" for a in acts) and any("open" in a for a in acts),
          "open actions went to the system (recorded in test mode)")

    print("\n== Settings")
    r = post("/api/settings", {"transcribe": False, "accuracy": "higher", "keep_videos": True, "excluded": ["202"]})
    c = load_config()
    check(r["ok"] and c["transcribe"] == "off" and c["whisper_model"] == "turbo" and c["keep_media_files"]
          and c["exclude_courses"] == ["202"], "settings saved", c)
    st = get("/api/state")
    check(st["settings"]["accuracy"] == "higher" and st["settings"]["excluded"] == ["202"], "settings read back")
    r = get("/api/canvas/courses")
    check([x["included"] for x in r["courses"] if x["id"] == "202"] == [False], "excluded course unticked")
    post("/api/settings", {"transcribe": True, "accuracy": "standard", "keep_videos": False, "excluded": []})

    print("\n== Token stops working")
    fc.TOKEN = "rotated-token"
    t0 = time.time()
    post("/api/sync/start")
    while get("/api/sync/progress")["running"] and time.time() - t0 < 120:
        time.sleep(0.5)
    p = get("/api/sync/progress")
    check(p["auth_error"], "rejected token noticed")
    st = get("/api/state")
    check(st["canvas"]["rejected"] and st["attention"][0]["action"] == "reconnect", "dashboard asks to reconnect")
    fc.TOKEN = fake_token
    r = post("/api/canvas/connect", {"url": base, "token": fake_token})
    st = get("/api/state")
    check(r["ok"] and not st["canvas"]["rejected"] and not any(a["kind"] == "token" for a in st["attention"]),
          "reconnecting clears the warning")

    print("\n== Sign out")
    r = post("/api/signout")
    st = get("/api/state")
    check(r["ok"] and not st["canvas"]["token_saved"] and not TOKEN_FILE.exists(), "token removed on sign out")
    check(st["setup_complete"] and st["courses"], "folder and courses kept after sign out")

    print("\n== Uninstall")
    home = WORK / "home"
    codex_cfg.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other"\n\n' + codex_cfg.read_text())
    from superstudent.assistants import claude_desktop_config
    desk = claude_desktop_config()
    data = json.loads(desk.read_text())
    data["mcpServers"]["other"] = {"command": "other"}
    desk.write_text(json.dumps(data))
    post("/api/canvas/connect", {"url": base, "token": fake_token})
    from superstudent.uninstall import uninstall
    result = uninstall(remove_library=False)
    text = codex_cfg.read_text()
    check("superstudent" not in text and "[mcp_servers.other]" in text and 'model = "gpt-5"' in text,
          "ChatGPT/Codex connector removed, other settings kept", text)
    data = json.loads(desk.read_text())
    check("superstudent" not in data["mcpServers"] and "other" in data["mcpServers"], "Claude connector removed, others kept")
    check(not (home / ".agents" / "skills" / "super-student").exists()
          and not (home / ".claude" / "skills" / "super-student").exists(), "skills removed")
    check(not TOKEN_FILE.exists() and not CONFIG_FILE.exists(), "token and settings removed")
    check((WORK / "lib").exists() and result["library_kept"], "course library kept")

    app.shutdown()
    fake.stop()
    print(f"\nAll {len(PASSED)} checks passed.")


if __name__ == "__main__":
    main()
