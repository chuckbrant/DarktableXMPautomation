#!/bin/bash
# Apply a Lightroom/Camera Raw XMP preset to a photo, either interactively
# (opens the photo directly in darktable's darkroom with the edits applied)
# or headlessly to a rendered output file with -o.
#
# Usage:
#   apply-lr-preset.sh [-p 1-100] <preset.xmp> <photo>                 (interactive darkroom)
#   apply-lr-preset.sh [-p 1-100] -o <output> <preset.xmp> <photo>     (headless render)
#
# -p sets preset strength, like Lightroom's Amount slider (default: 50).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DARKTABLE_BIN="/Applications/darktable.app/Contents/MacOS/darktable"
DARKTABLE_CLI_BIN="/Applications/darktable.app/Contents/MacOS/darktable-cli"

print_help() {
  cat <<'HELP'
Usage:
  apply-lr-preset.sh [-p 1-100] <preset.xmp> <photo>
  apply-lr-preset.sh [-p 1-100] -o <output> <preset.xmp> <photo>

Applies a Lightroom/Camera Raw .xmp preset to a photo in darktable.

  <preset.xmp>   Lightroom/ACR XMP preset file
  <photo>        Photo to apply it to

Options:
  -p 1-100    Preset strength, like Lightroom's Amount slider (default: 50)
  -o output   Render headlessly to this file instead of opening darktable's
              darkroom interactively (no GUI interaction needed)
  -h          Show this help

Examples:
  apply-lr-preset.sh mypreset.xmp photo.jpg
  apply-lr-preset.sh -p 75 mypreset.xmp photo.jpg
  apply-lr-preset.sh -p 100 -o out.jpg mypreset.xmp photo.jpg

Note: every -o run force-quits any darktable process currently running on
this machine before starting, since darktable only allows one instance.
HELP
}

OUTPUT_PATH=""
PERCENT=50
while getopts "o:p:h" opt; do
  case "$opt" in
    o) OUTPUT_PATH="$OPTARG" ;;
    p) PERCENT="$OPTARG" ;;
    h) print_help; exit 0 ;;
    *) print_help >&2; exit 1 ;;
  esac
done
shift $((OPTIND - 1))

if [ $# -ne 2 ]; then
  print_help >&2
  exit 1
fi

if ! [[ "$PERCENT" =~ ^[0-9]+$ ]] || [ "$PERCENT" -lt 1 ] || [ "$PERCENT" -gt 100 ]; then
  echo "-p must be an integer 1-100, got: $PERCENT" >&2
  exit 1
fi

XMP_PATH="$1"
PHOTO_PATH="$2"

if [ ! -f "$XMP_PATH" ]; then
  echo "Preset not found: $XMP_PATH" >&2
  exit 1
fi
if [ ! -f "$PHOTO_PATH" ]; then
  echo "Photo not found: $PHOTO_PATH" >&2
  exit 1
fi

DT_CONFIG_DIR="$HOME/.config/darktable"
DT_LUA_DIR="$DT_CONFIG_DIR/lua"
DT_LUARC="$DT_CONFIG_DIR/luarc"

# Self-install: copy the Lua script into darktable's config and register it
# in luarc, if that hasn't already been done.
mkdir -p "$DT_LUA_DIR"
if ! cmp -s "$SCRIPT_DIR/lua/apply_xmp_preset.lua" "$DT_LUA_DIR/apply_xmp_preset.lua" 2>/dev/null; then
  cp "$SCRIPT_DIR/lua/apply_xmp_preset.lua" "$DT_LUA_DIR/apply_xmp_preset.lua"
  echo "Installed apply_xmp_preset.lua to $DT_LUA_DIR"
fi
if [ ! -f "$DT_LUARC" ] || ! grep -q 'require "apply_xmp_preset"' "$DT_LUARC"; then
  echo 'require "apply_xmp_preset"' >> "$DT_LUARC"
  echo "Registered apply_xmp_preset in $DT_LUARC"
fi

WORK_DIR="${DT_XMP_WORK_DIR:-/tmp/darktable-xmp-automation}"
mkdir -p "$WORK_DIR"
ACTIONS_JSON="$WORK_DIR/actions_$$.json"

# darktable only allows one running instance -- a second launch silently
# hands off to whatever instance is already running via IPC instead of
# starting fresh (confirmed empirically 2026-08-23: this caused our tracked
# process to sit alive-but-inert for a full timeout window while a stale,
# untracked instance from an earlier run did the real work on its own
# schedule). Force a clean slate every run.
pkill -9 -f "/Applications/darktable.app/Contents/MacOS/darktable" 2>/dev/null || true
sleep 1
find ~/.config/darktable -maxdepth 1 -iname "lock*" -delete 2>/dev/null || true

python3 "$SCRIPT_DIR/xmp_to_darktable.py" "$XMP_PATH" "$ACTIONS_JSON" --percent "$PERCENT"
ACTIONS_TXT="${ACTIONS_JSON%.json}.txt"

if [ -z "$OUTPUT_PATH" ]; then
  echo "Launching darktable on: $PHOTO_PATH (strength: ${PERCENT}%)"
  DT_XMP_PRESET_ACTIONS="$ACTIONS_TXT" "$DARKTABLE_BIN" "$PHOTO_PATH"
  exit 0
fi

# --- Headless flow ---
# darktable's own sidecar path: the full photo filename (extension kept) with
# .xmp appended -- e.g. photo.jpg -> photo.jpg.xmp, NOT photo.xmp.
XMP_SIDECAR="${PHOTO_PATH}.xmp"

# Never delete a pre-existing sidecar (it may hold real prior edits); track
# its mtime instead so we can tell when darktable has (re)written it.
BEFORE_MTIME=0
if [ -f "$XMP_SIDECAR" ]; then
  BEFORE_MTIME=$(stat -f %m "$XMP_SIDECAR")
fi

# darktable 5.6.0 has an intermittent, pre-existing GTK-internal
# memory-corruption crash triggered probabilistically by rapid dt.gui.action
# automation (confirmed empirically 2026-08-23: identical actions against
# the same freshly-restarted darktable sometimes ran clean, sometimes
# crashed -- not deterministically tied to any specific action, mapping, or
# activation method we could find, after extensive isolation attempts). Not
# something fixable from this script's side. So: retry on crash rather than
# fail outright -- a retry after a crash has consistently succeeded in
# testing.
MAX_ATTEMPTS=3
attempt=1
APPLIED=0
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  echo "Applying preset to $PHOTO_PATH (headless, strength: ${PERCENT}%, attempt $attempt/$MAX_ATTEMPTS)..."
  pkill -9 -f "/Applications/darktable.app/Contents/MacOS/darktable" 2>/dev/null || true
  sleep 1
  find ~/.config/darktable -maxdepth 1 -iname "lock*" -delete 2>/dev/null || true

  DT_XMP_PRESET_ACTIONS="$ACTIONS_TXT" "$DARKTABLE_BIN" "$PHOTO_PATH" &
  DT_PID=$!

  # A backgrounded, never-focused GUI process is subject to macOS App Nap,
  # which throttles its timers (confirmed empirically 2026-08-23: without
  # this, darktable's own auto-save-sidecar timer didn't fire within 60s).
  # Activating it keeps that timer running promptly.
  #
  # Activate by PID via System Events, NOT `tell application "darktable" to
  # activate` -- the latter appeared to make the crash above more likely
  # when combined with this script's flow, while activating by PID did not,
  # across extensive testing 2026-08-23 (though given the crash's
  # intermittent nature, this is not a fully confirmed cause -- just the
  # safer of the two known-working options).
  sleep 1
  timeout 8 osascript -e "
tell application \"System Events\"
  set frontmost of first process whose unix id is $DT_PID to true
end tell" >/dev/null 2>&1 || true

  # darktable's own autosave (history -> database + .xmp sidecar) is
  # reactive, not on an independent timer, and its first possible flush is
  # ~20s after darkroom entry (see apply_xmp_preset.lua's trailing
  # flush-trigger, added 2026-08-23 after root-causing this by instrumenting
  # darktable's own C source). So: poll for the sidecar to appear/update --
  # comfortably past that ~20s mark -- then confirm its size is stable
  # (write finished) before touching the process.
  TIMEOUT_S=90
  elapsed=0
  NEW_MTIME=0
  CRASHED=0
  while :; do
    if ! kill -0 "$DT_PID" 2>/dev/null; then
      echo "darktable exited before writing the sidecar (crash on attempt $attempt)" >&2
      CRASHED=1
      break
    fi
    if [ -f "$XMP_SIDECAR" ]; then
      NEW_MTIME=$(stat -f %m "$XMP_SIDECAR")
      if [ "$NEW_MTIME" -gt "$BEFORE_MTIME" ]; then
        APPLIED=1
        break
      fi
    fi
    if [ "$elapsed" -ge "$TIMEOUT_S" ]; then
      echo "Timed out waiting for darktable to write the sidecar" >&2
      kill -9 "$DT_PID" 2>/dev/null || true
      exit 1
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  if [ "$APPLIED" -eq 1 ]; then
    break
  fi
  attempt=$((attempt + 1))
done

if [ "$APPLIED" -ne 1 ]; then
  echo "darktable crashed on all $MAX_ATTEMPTS attempts" >&2
  exit 1
fi

# Confirm the write has settled (size unchanged across two checks).
PREV_SIZE=-1
for _ in $(seq 1 10); do
  CUR_SIZE=$(stat -f %z "$XMP_SIDECAR")
  if [ "$CUR_SIZE" -eq "$PREV_SIZE" ]; then
    break
  fi
  PREV_SIZE="$CUR_SIZE"
  sleep 1
done

kill -9 "$DT_PID" 2>/dev/null || true
wait "$DT_PID" 2>/dev/null || true

echo "Rendering to $OUTPUT_PATH via darktable-cli..."
"$DARKTABLE_CLI_BIN" "$PHOTO_PATH" "$XMP_SIDECAR" "$OUTPUT_PATH"
echo "Done: $OUTPUT_PATH"
