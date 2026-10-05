"""Build "Super Student.app" and a zip to hand to Mac users.

    python mac/build_app.py            -> dist/Super Student.app, dist/SuperStudent-<version>-mac.zip

The app is small: a launcher script, Super Student as a ready-built package (a wheel, so nothing is ever
built or written inside the app on a student's Mac), the exact versions of everything it depends on for each
kind of Mac (Apple Silicon, Intel), all available as ready-made downloads for macOS 13 and later, and pinned
checksums for the installer (uv) it downloads from PyPI on first run to set up a private Python in
~/.superstudent.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

UV_VERSION = "0.12.21"
UV_PINS = {  # platform: (sha256, url) of the official uv wheels on PyPI
    "macos-arm64": ("eb4f75e8ed770e1f142e03a134b9507e7f4710c4b2cec385d4bf8c5810a91094",
                    "https://files.pythonhosted.org/packages/66/72/389b430ec12bd15547d4d3641622479cbf7a5c94af0ffb14fee2ed58f891/uv-0.12.21-py3-none-macosx_11_0_arm64.whl"),
    "macos-x86_64": ("9c6fab087f35c8f8c0c79ad4c8a94f6804b983369c2887250fb753a70d8080fd",
                     "https://files.pythonhosted.org/packages/51/f0/8673a5aeb771f99aadd269aa069874b10b8ed43fcb3a08a88d3770c27ad9/uv-0.12.21-py3-none-macosx_10_12_x86_64.whl"),
    # Linux entries let the launcher be tested on a Linux build machine.
    "linux-x86_64": ("0083454d25479f1cd03547c8c98c235b4af92bd07f0e61c3eae5d1cbb0c1834c",
                     "https://files.pythonhosted.org/packages/c6/2b/8700206efe193fc279fe32c11ab9a502c8364e7882e3f6f36a00a6cc8af7/uv-0.12.21-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"),
    "linux-aarch64": ("b7db8b4913e6cd34e6aabbe08ff82fbac7c1edae57747ea23118ca460f060d44",
                      "https://files.pythonhosted.org/packages/0e/05/6c536d9cf7977d3c55b8835a90d1e9e58def00309bec0e2ad062157d4499/uv-0.12.21-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64.whl"),
}

MAC_TARGETS = {"arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}   # `uname -m` -> uv platform
MIN_MACOS = "13.0"
EXTRAS = ("media", "gui")
SOURCE_ONLY = ("proxy-tools",)     # pure-Python packages published without a wheel (safe to build anywhere)

HOW_TO_INSTALL = """How to install Super Student on your Mac
=========================================

Personal use only while Canvas integration approval is unresolved. Connect only your own account
and follow your institution's rules. Do not onboard other users through personal access tokens.

1. Drag "Super Student" into your Applications folder.

2. Open it from Applications.

   The first time, macOS says it can't check the app (Super Student isn't from the App Store).
   - macOS 15 Sequoia or newer: click Done. Open System Settings > Privacy & Security, scroll down to
     the message about Super Student, click Open Anyway, and confirm. Then open Super Student again.
   - macOS 14 Sonoma or older: Control-click (or right-click) Super Student, choose Open, then Open.
   You only need to do this once.

3. Click Set up. Super Student downloads what it needs (about 300 MB, 3 to 10 minutes) and then
   opens by itself.

4. Follow the steps in the window: your school, a Canvas access token, your courses, and whether you
   study with ChatGPT, Claude or both.

Updating an older copy: quit Super Student, replace it in Applications with this copy, then open it.
Its private runtime updates automatically; your course library and settings are kept. Restart ChatGPT
and Claude afterward so their connectors use the new version.

To remove it later: open Super Student > Settings > Uninstall, then drag the app to the Trash.
"""


def version() -> str:
    text = (ROOT / "superstudent" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def build_wheel(dest: Path) -> Path:
    """Super Student as a wheel, built from a clean copy of the source (so the source folder stays clean)."""
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ss-wheel-") as tmp:
        src = Path(tmp) / "src"
        src.mkdir()
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(ROOT / name, src / name)
        shutil.copytree(ROOT / "superstudent", src / "superstudent",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "--wheel-dir", str(dest), str(src)],
                       check=True)
    wheels = sorted(dest.glob("superstudent-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one superstudent wheel in {dest}, found {wheels}")
    return wheels[0]


def build_uv() -> Path:
    """The pinned uv for this build machine (to work out the dependency versions)."""
    import platform

    tag = {("Linux", "x86_64"): "linux-x86_64", ("Linux", "aarch64"): "linux-aarch64",
           ("Darwin", "arm64"): "macos-arm64", ("Darwin", "x86_64"): "macos-x86_64"}.get((platform.system(), platform.machine()))
    if not tag:
        raise SystemExit("Build on macOS or Linux.")
    cache = ROOT / "dist" / ".build-cache"
    cache.mkdir(parents=True, exist_ok=True)
    uv = cache / f"uv-{UV_VERSION}-{tag}"
    if uv.exists():
        return uv
    sha, url = UV_PINS[tag]
    data = urllib.request.urlopen(url, timeout=120).read()
    if hashlib.sha256(data).hexdigest() != sha:
        raise SystemExit("uv download checksum mismatch")
    whl = cache / "uv.whl"
    whl.write_bytes(data)
    with zipfile.ZipFile(whl) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".data/scripts/uv"))
        uv.write_bytes(zf.read(member))
    uv.chmod(0o755)
    whl.unlink()
    return uv


def lock_dependencies(res: Path) -> None:
    """constraints-<arch>.txt: the exact version of every dependency, chosen so each one has a ready-made
    download (a wheel) for that kind of Mac on macOS 13 or later. Nothing has to be compiled on a student's
    Mac (which would need developer tools they don't have), and every student gets the same tested set."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs = list(project["dependencies"])
    for extra in EXTRAS:
        reqs += project["optional-dependencies"][extra]
    uv = build_uv()
    with tempfile.TemporaryDirectory(prefix="ss-lock-") as tmp:
        req_in = Path(tmp) / "requirements.in"
        req_in.write_text("\n".join(reqs) + "\n", encoding="utf-8")
        for arch, platform_tag in MAC_TARGETS.items():
            out = res / f"constraints-{arch}.txt"
            cmd = [str(uv), "pip", "compile", str(req_in), "--quiet", "--python-version", "3.12",
                   "--python-platform", platform_tag, "--only-binary", ":all:", "--no-header", "--no-annotate",
                   "--output-file", str(out)]
            for name in SOURCE_ONLY:
                cmd += ["--no-binary", name]
            env = dict(os.environ, MACOSX_DEPLOYMENT_TARGET=MIN_MACOS, UV_CACHE_DIR=str(ROOT / "dist" / ".build-cache" / "uv"))
            subprocess.run(cmd, check=True, env=env)
            pins = out.read_text(encoding="utf-8")
            out.write_text(f"# Exact versions for {platform_tag}, macOS {MIN_MACOS}+ (made by mac/build_app.py)\n" + pins,
                           encoding="utf-8")


def build(out_dir: Path) -> Path:
    from make_icon import draw

    ver = version()
    app = out_dir / "Super Student.app"
    shutil.rmtree(app, ignore_errors=True)
    contents = app / "Contents"
    macos, res = contents / "MacOS", contents / "Resources"
    macos.mkdir(parents=True)
    res.mkdir(parents=True)

    info = {
        "CFBundleName": "Super Student",
        "CFBundleDisplayName": "Super Student",
        "CFBundleIdentifier": "app.superstudent.mac",
        "CFBundleVersion": ver,
        "CFBundleShortVersionString": ver,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "SuperStudent",
        "CFBundleIconFile": "icon",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSApplicationCategoryType": "public.app-category.education",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Super Student reads your Canvas courses for studying; it never submits or edits anything.",
    }
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")

    launcher = macos / "SuperStudent"
    shutil.copy2(HERE / "launcher.sh", launcher)
    launcher.chmod(0o755)
    shutil.copy2(HERE / "progress.js", res / "progress.js")
    image = draw(1024)
    image.save(res / "icon.png")
    image.save(res / "icon.icns", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
    (res / "VERSION").write_text(ver + "\n", encoding="utf-8")
    (res / "uv-version").write_text(UV_VERSION + "\n", encoding="utf-8")
    (res / "uv-pins.txt").write_text("".join(f"{tag} {sha} {url}\n" for tag, (sha, url) in UV_PINS.items()),
                                     encoding="utf-8")
    build_wheel(res / "payload")
    lock_dependencies(res)
    return app


def zip_app(app: Path, zip_path: Path) -> None:
    """Zip with Unix permissions kept, so the launcher stays executable after unzipping on a Mac."""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted([app, *app.rglob("*")]):
            rel = path.relative_to(app.parent).as_posix()
            if path.is_dir():
                zi = zipfile.ZipInfo(rel + "/", date_time=time.localtime(path.stat().st_mtime)[:6])
                zi.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                zf.writestr(zi, b"")
            else:
                zi = zipfile.ZipInfo.from_file(path, rel)
                mode = 0o755 if os.access(path, os.X_OK) else 0o644
                zi.external_attr = (stat.S_IFREG | mode) << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(zi, path.read_bytes())
        zi = zipfile.ZipInfo("How to install.txt", date_time=time.localtime()[:6])
        zi.external_attr = (stat.S_IFREG | 0o644) << 16
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, HOW_TO_INSTALL)


def main() -> None:
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    app = build(out)
    zip_path = out / f"SuperStudent-{version()}-mac.zip"
    zip_app(app, zip_path)
    print(f"Built {app}\nZipped {zip_path} ({zip_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
