#!/bin/bash
# Super Student for Mac.
#
# The first time it runs, this sets up a private copy of Python and Super Student inside ~/.superstudent
# (nothing else on the Mac is changed), then opens the app window. Later runs open straight away.
# A newer copy of the app updates that private copy the first time it's opened.
# Written for the bash 3.2 that comes with macOS.
set -u
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$(cd "$HERE/../Resources" && pwd)"
SS_HOME="${SUPERSTUDENT_HOME:-$HOME/.superstudent}"
RUNTIME="$SS_HOME/runtime"
PY="$RUNTIME/bin/python"
LOGDIR="$SS_HOME/logs"
LOG="$LOGDIR/install.log"
STATUS="$SS_HOME/install-status"
LOCK="$SS_HOME/install.lock"
WANT="$(cat "$RES/VERSION")"
ICON="$RES/icon.icns"
mkdir -p "$LOGDIR"

# Pass through only our own options (older macOS adds -psn_… when it opens an app).
ARGS=()
for a in "$@"; do case "$a" in -psn_*) ;; *) ARGS+=("$a") ;; esac; done

have_dialogs() { [ -z "${SS_NO_DIALOGS:-}" ] && command -v osascript >/dev/null 2>&1; }

# ask "message" "Button A|Button B" "Default"  -> prints the button clicked ("" if closed/cancelled)
ask() {
  if ! have_dialogs; then echo "$3"; return; fi
  osascript - "$1" "$2" "$3" "$ICON" <<'OSA' 2>/dev/null
on run argv
  set AppleScript's text item delimiters to "|"
  set btns to text items of (item 2 of argv)
  try
    set theIcon to (POSIX file (item 4 of argv)) as alias
    set r to display dialog (item 1 of argv) buttons btns default button (item 3 of argv) with title "Super Student" with icon theIcon
  on error number n
    if n is -128 then return ""
    set r to display dialog (item 1 of argv) buttons btns default button (item 3 of argv) with title "Super Student"
  end try
  return button returned of r
end run
OSA
}

status() { printf '%s\n' "$1" > "$STATUS"; echo "== $1" >> "$LOG"; }

python_works() { [ -x "$PY" ] && "$PY" -c '' >/dev/null 2>&1; }

installed_ok() {
  python_works || return 1
  "$PY" - "$WANT" <<'PYCHECK' >/dev/null 2>&1
import importlib.util, platform, sys
import superstudent
ok = superstudent.__version__ == sys.argv[1]
if platform.system() == "Darwin":
    ok = ok and importlib.util.find_spec("webview") is not None
sys.exit(0 if ok else 1)
PYCHECK
}

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi
}

free_mb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 {print int($4 / 1024)}'; }

# fail "what went wrong" ["what to do"]: the advice is worked out from the log when it isn't given.
fail() {
  local advice="${2:-}"
  status "failed"
  echo "FAILED: $1" >> "$LOG"
  if [ -z "$advice" ]; then
    if grep -qi "no space left on device" "$LOG" 2>/dev/null; then
      advice="This Mac is out of disk space. Free up at least 2 GB, then open Super Student again."
    elif grep -qi "read-only file system" "$LOG" 2>/dev/null; then
      advice="macOS didn't let Super Student save its files. Move Super Student into your Applications folder, then open it again."
    else
      advice="Check that this Mac is connected to the internet, then open Super Student again."
    fi
  fi
  if have_dialogs; then
    choice="$(ask "Super Student couldn't finish setting up: $1

$advice" "Show details|OK" "OK")"
    [ "$choice" = "Show details" ] && open -e "$LOG"
  else
    echo "Super Student couldn't finish setting up: $1 $advice (details: $LOG)" >&2
  fi
  rm -rf "$LOCK"
  exit 1
}

# Apps opened from the Finder don't get the proxy set in System Settings; curl and uv need it spelled out.
use_system_proxy() {
  local info host port
  [ -n "${HTTPS_PROXY:-}${https_proxy:-}" ] && return 0
  command -v scutil >/dev/null 2>&1 || return 0
  info="$(scutil --proxy 2>/dev/null)" || return 0
  echo "$info" | grep -q "HTTPSEnable : 1" || return 0
  host="$(echo "$info" | awk '$1 == "HTTPSProxy" {print $3}')"
  port="$(echo "$info" | awk '$1 == "HTTPSPort" {print $3}')"
  [ -n "$host" ] || return 0
  export HTTPS_PROXY="http://$host:${port:-443}" https_proxy="http://$host:${port:-443}"
  echo "Using the proxy from System Settings: $host:${port:-443}" >> "$LOG"
}

# An open window from an older version would keep running the old code: ask it to close first.
close_running_window() {
  local info="$SS_HOME/app-window.json" port token n
  [ -f "$info" ] || return 0
  port="$(sed -n 's/.*"port": *\([0-9][0-9]*\).*/\1/p' "$info" | head -n 1)"
  token="$(sed -n 's/.*"token": *"\([^"]*\)".*/\1/p' "$info" | head -n 1)"
  [ -n "$port" ] && [ -n "$token" ] || return 0
  curl -s -m 3 -X POST -H "Content-Type: application/json" -H "X-SS-Token: $token" -d '{}' \
    "http://127.0.0.1:$port/api/quit" >/dev/null 2>&1 || return 0
  n=0
  while [ -f "$info" ] && [ "$n" -lt 20 ]; do sleep 0.5; n=$((n + 1)); done
}

get_uv() {
  local os arch tag line url sum tmp
  os="$(uname -s)"; arch="$(uname -m)"
  case "$os-$arch" in
    Darwin-arm64) tag="macos-arm64" ;;
    Darwin-x86_64) tag="macos-x86_64" ;;
    Linux-x86_64) tag="linux-x86_64" ;;
    Linux-aarch64) tag="linux-aarch64" ;;
    *) fail "this computer ($os $arch) isn't supported." "Super Student runs on Macs with macOS 13 or newer." ;;
  esac
  UV="$SS_HOME/bin/uv"
  if [ -x "$UV" ] && [ "$("$UV" --version 2>/dev/null | awk '{print $2}')" = "$(cat "$RES/uv-version")" ]; then return; fi
  line="$(grep "^$tag " "$RES/uv-pins.txt")" || fail "no installer for $tag."
  sum="$(echo "$line" | awk '{print $2}')"; url="$(echo "$line" | awk '{print $3}')"
  tmp="$(mktemp -d)"
  status "Downloading the installer"
  curl -fsSL --retry 3 -o "$tmp/uv.whl" "$url" >> "$LOG" 2>&1 || fail "couldn't download the installer."
  [ "$(sha256_of "$tmp/uv.whl")" = "$sum" ] || fail "the installer download was damaged (checksum mismatch)."
  mkdir -p "$SS_HOME/bin"
  (cd "$tmp" && unzip -q -o uv.whl '*.data/scripts/uv') >> "$LOG" 2>&1 || fail "couldn't unpack the installer."
  mv -f "$tmp"/*.data/scripts/uv "$UV" && chmod 755 "$UV"
  rm -rf "$tmp"
}

install_runtime() {
  local first="$1" progress_pid="" extras="media" need wheel pins free system
  : > "$LOG"
  system="$(sw_vers -productVersion 2>/dev/null)"
  if [ -n "$system" ]; then system="macOS $system"; else system="$(uname -sr)"; fi
  echo "Super Student $WANT setup, $(date), $(uname -m), $system" >> "$LOG"
  status "Starting"
  need=600; [ "$first" = "first" ] && need=1500
  free="$(free_mb "$SS_HOME")"
  if [ -n "$free" ] && [ "$free" -lt "$need" ]; then
    fail "there isn't enough free disk space (${free} MB free, about ${need} MB needed)." \
         "Free up some space (empty the Trash, delete large downloads), then open Super Student again."
  fi
  if have_dialogs; then
    osascript -l JavaScript "$RES/progress.js" "$STATUS" "$RES/icon.png" "$first" >/dev/null 2>&1 &
    progress_pid=$!
  fi
  use_system_proxy
  export UV_PYTHON_INSTALL_DIR="$SS_HOME/python" UV_CACHE_DIR="$SS_HOME/cache" UV_NO_PROGRESS=1
  get_uv
  if ! python_works; then
    status "Downloading Python (about 20 MB)"
    if [ -n "${SS_PYTHON:-}" ]; then   # tests: use an existing Python instead of downloading one
      "$UV" venv --clear --python "$SS_PYTHON" "$RUNTIME" >> "$LOG" 2>&1 || fail "couldn't set up Python."
    else
      "$UV" venv --clear --python 3.12 --python-preference only-managed "$RUNTIME" >> "$LOG" 2>&1 \
        || fail "couldn't download Python."
    fi
  fi
  [ "$(uname -s)" = "Darwin" ] && extras="media,gui"
  wheel=""
  for w in "$RES"/payload/superstudent-*.whl; do [ -f "$w" ] && wheel="$w" && break; done
  [ -n "$wheel" ] || fail "this copy of the app is incomplete." "Download Super Student again and replace the app."
  # Ready-made packages only, at the exact versions tested for this kind of Mac: nothing gets compiled here.
  set -- --python "$PY" --reinstall-package superstudent --only-binary ":all:" --no-binary proxy-tools
  pins="$RES/constraints-$(uname -m).txt"
  rm -f "$SS_HOME/constraints.txt"
  if [ -f "$pins" ] && [ "$(uname -s)" = "Darwin" ]; then
    cp -f "$pins" "$SS_HOME/constraints.txt"
    # Named relative to ~/.superstudent: uv splits this option's value at spaces ("Super Student.app").
    set -- "$@" --constraint constraints.txt
  fi
  status "Installing Super Student and the parts that read slides, PDFs and recordings (the longest step)"
  (cd "$SS_HOME" && "$UV" pip install "$@" "${wheel}[$extras]") >> "$LOG" 2>&1 || fail "couldn't install Super Student."
  status "Checking everything works"
  installed_ok || fail "the installed copy didn't start."
  "$UV" cache prune >> "$LOG" 2>&1
  # keep the command line tool handy for people who want it: ~/.superstudent/bin/superstudent
  ln -sf "$RUNTIME/bin/superstudent" "$SS_HOME/bin/superstudent"
  [ "$first" = "update" ] && echo "$WANT" > "$SS_HOME/just-updated"
  status "done"
  if [ -n "$progress_pid" ]; then sleep 1; kill "$progress_pid" 2>/dev/null; fi
}

case "$HERE" in
  */AppTranslocation/*)   # opened straight from Downloads: macOS runs it from a temporary copy
    ask "Super Student is running from your Downloads folder. Drag it into your Applications folder (then open it from there) so it keeps working after a restart." "OK" "OK" >/dev/null ;;
esac

if ! installed_ok; then
  # One setup at a time (e.g. if the app is double-clicked twice).
  mkdir -p "$SS_HOME"
  if ! mkdir "$LOCK" 2>/dev/null; then
    other="$(cat "$LOCK/pid" 2>/dev/null)"
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null && \
       ps -p "$other" -o command= 2>/dev/null | grep -Eq "SuperStudent|launcher"; then
      ask "Super Student is still getting ready. It opens by itself when it's done." "OK" "OK" >/dev/null
      exit 0
    fi
    rm -rf "$LOCK"; mkdir "$LOCK"
  fi
  echo $$ > "$LOCK/pid"
  if [ -d "$RUNTIME" ] && ! python_works; then
    # e.g. moved to a new Mac: the old private Python can't run here. Rebuild it (courses and settings stay).
    echo "The private Python doesn't run on this Mac; setting it up again." >> "$LOG"
    rm -rf "$RUNTIME" "$SS_HOME/python"
  fi
  if [ -d "$RUNTIME" ]; then first="update"; else first="first"; fi
  if [ "$first" = "first" ]; then
    choice="$(ask "Super Student needs to finish setting up before it opens for the first time.

It downloads about 300 MB and takes 3 to 10 minutes. Keep this Mac connected to the internet. Super Student opens by itself when it's ready." "Not now|Set up" "Set up")"
    if [ "$choice" != "Set up" ]; then rm -rf "$LOCK"; exit 0; fi
  else
    close_running_window
  fi
  install_runtime "$first"
  rm -rf "$LOCK"
fi

exec "$PY" -m superstudent gui ${ARGS[@]+"${ARGS[@]}"}
