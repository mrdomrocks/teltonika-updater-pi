#!/usr/bin/env bash
# Update TUA on a Raspberry Pi from the Cursor git remote. GitHub is not used.
#
#   sudo ./install/update.sh
#   sudo ./install/update.sh cursor/remove-rms-lan-actions-3854
#   ./install/update.sh --user
#
# The first run with no ref uses main. After that, the ref is remembered in
# the source checkout and the next run fetches the same one.
set -euo pipefail

REMOTE="${RUTKEEPER_GIT_REMOTE:-https://origin.cursor.com/git/mrdomrocks/teltonika-updater-pi.git}"
MODE="system"
REF=""
for arg in "$@"; do
  case "$arg" in
    --user) MODE="user" ;;
    -*)
      echo "Unknown option: $arg" >&2
      exit 1
      ;;
    *)
      if [[ -n "$REF" ]]; then
        echo "Pass one git ref, or none to reuse the last one." >&2
        exit 1
      fi
      REF="$arg"
      ;;
  esac
done

if [[ "$MODE" == "system" ]]; then
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run sudo ./install/update.sh, or ./install/update.sh --user." >&2
    exit 1
  fi
  SRC="${RUTKEEPER_SRC:-/opt/rutkeeper-src}"
else
  SRC="${RUTKEEPER_SRC:-$HOME/.local/src/rutkeeper}"
fi

if ! command -v git >/dev/null 2>&1; then
  if [[ "$MODE" == "system" ]] && command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y git ca-certificates
  else
    echo "Install git, then run this again." >&2
    exit 1
  fi
fi

mkdir -p "$(dirname "$SRC")"
if [[ ! -d "$SRC/.git" ]]; then
  git clone --origin cursor "$REMOTE" "$SRC"
else
  if git -C "$SRC" remote get-url cursor >/dev/null 2>&1; then
    git -C "$SRC" remote set-url cursor "$REMOTE"
  else
    git -C "$SRC" remote add cursor "$REMOTE"
  fi
fi

if [[ -z "$REF" && -f "$SRC/.cursor-ref" ]]; then
  REF="$(tr -d '[:space:]' < "$SRC/.cursor-ref")"
fi
REF="${REF:-${RUTKEEPER_GIT_REF:-main}}"

git -C "$SRC" fetch cursor "$REF"
git -C "$SRC" checkout --detach --force FETCH_HEAD
printf '%s\n' "$REF" > "$SRC/.cursor-ref"

echo "Installing $(git -C "$SRC" rev-parse --short HEAD) from $REMOTE ($REF)."
if [[ "$MODE" == "user" ]]; then
  "$SRC/install/install.sh" --user
else
  "$SRC/install/install.sh"
fi
