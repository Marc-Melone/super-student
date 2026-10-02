"""Run the app against the fake Canvas so it can be clicked through in a browser (used for screenshots).

    python tests/gui_demo.py [port]

Prints one JSON line with the page address, then serves until stopped. Canvas token for the demo:
test-token-123-padding-to-look-real
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

WORK = Path(os.environ.get("SS_TEST_DIR") or tempfile.mkdtemp(prefix="ss-demo-"))
for sub in ("app", "lib", "home"):
    shutil.rmtree(WORK / sub, ignore_errors=True)
(WORK / "home" / "Applications" / "ChatGPT.app").mkdir(parents=True)   # pretend ChatGPT is installed
os.environ.update(HOME=str(WORK / "home"), SUPERSTUDENT_HOME=str(WORK / "app"), SUPERSTUDENT_APP_DRYRUN="1")
for var in ("CANVAS_TOKEN", "SUPERSTUDENT_TOKEN", "SUPERSTUDENT_CANVAS_URL", "SUPERSTUDENT_LIBRARY", "CODEX_HOME"):
    os.environ.pop(var, None)

import fake_canvas as fc  # noqa: E402
from make_fixtures import build_all  # noqa: E402

fc.TOKEN = "test-token-123-padding-to-look-real"
fake = fc.FakeCanvas(build_all(WORK / "fixtures"))
base = fake.start()
os.environ["SUPERSTUDENT_ACCOUNT_SEARCH"] = base + "/api/v1/accounts/search"

from superstudent.config import load_config, save_config  # noqa: E402
from superstudent.gui.app import App  # noqa: E402

cfg = load_config()
cfg["library_dir"] = str(WORK / "home" / "SuperStudent")
save_config(cfg)
app = App(native=False)
app.serve(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
print(json.dumps({"url": app.url(), "canvas": base, "work": str(WORK)}), flush=True)
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
