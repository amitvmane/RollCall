#!/usr/bin/env bash
#
# scripts/uninstall.sh -- what `make uninstall` runs: take this app off the
# machine WITHOUT touching its data.
#
# Removes: this app's containers (stopped, then deleted) and the networks
# compose made for them -- so nothing of it runs, or comes back after a
# reboot. With REMOVE_IMAGES=1, its images too (they're downloaded or
# rebuilt again on the next `make start`).
#
# Never removes, in any mode: the data folder (DATA_KEY in .env), named
# volumes (`down` is never given -v), .env, or any file in this folder.
# There is no flag that deletes data -- that is only ever done by hand.
# After removing, it checks the data folder and volumes are still there and
# says so; if they somehow aren't, it fails loudly.
#
# The same file in every app's repo; the Makefile says what differs:
#   COMPOSE       the compose command, with -f/--profile (every profile, so
#                 no container is missed)
#   DATA_KEY      the .env key naming the data folder (DATA_ROOT, DATA_DIR)
#   DATA_DEFAULT  its value when .env doesn't set it (relative = this folder)
#   ENV_FILE      default .env
# and the knobs:
#   PLAN=1           show what would happen, change nothing
#   CONFIRM=yes      don't ask (for scripts; asked otherwise)
#   REMOVE_IMAGES=1  also delete this app's images
#
# bash 3.2 compatible (macOS). No `| grep -q` / `| head` under pipefail
# (see scripts/test_shell.py, where it exists).

set -Eeuo pipefail
cd "$(dirname "$0")/.."
HERE="$(pwd -P)"

: "${COMPOSE:?COMPOSE not set -- run this through make uninstall}"
: "${DATA_KEY:?DATA_KEY not set -- run this through make uninstall}"
ENV_FILE="${ENV_FILE:-.env}"
PLAN="${PLAN:-}"
CONFIRM="${CONFIRM:-}"
REMOVE_IMAGES="${REMOVE_IMAGES:-}"
read -r -a compose <<< "$COMPOSE"

# Paths in this folder that are rebuilt by themselves -- caches and build
# output, never anything to keep. Matched against each path component.
REGENERABLE=" __pycache__ .ruff_cache .pytest_cache .mypy_cache node_modules dist .yarn @types .make-state .turbo coverage "

read_env() {
  local line=""
  [[ -f "$ENV_FILE" ]] || return 0
  line="$(grep -m1 "^$1=" "$ENV_FILE" || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# Top-level names in a folder, one line -- what must still be there after.
# shellcheck disable=SC2012  # names here are the app's own folder names
top_names() { ls -A "$1" 2>/dev/null | tr '\n' ' ' | sed 's/ $//' || true; }
# Files a database removes itself on a clean shutdown (SQLite's -wal/-shm,
# Postgres's postmaster.pid): gone after `down` is right, not data loss.
transient() { case "$1" in *-wal|*-shm|*-journal|*.pid|*.lock|*.sock) return 0;; esac; return 1; }
# Every non-transient name in "$2" (a top_names list) still in folder "$1"?
still_there() {
  local n
  for n in $2; do
    transient "$n" && continue
    [[ -e "$1/$n" ]] || return 1
  done
}
size_of() { du -sh "$1" 2>/dev/null | awk '{printf "%s", $1}' || true; }
lines() { printf '%s\n' "$@" | sed '/^$/d'; }
join() { local IFS=","; printf '%s' "$*" | sed 's/,/, /g'; }

is_regenerable() {
  local part rest="$1"
  rest="${rest%/}"
  while [[ -n "$rest" ]]; do
    part="${rest%%/*}"
    [[ "$REGENERABLE" == *" $part "* ]] && return 0
    [[ "$rest" == */* ]] || break
    rest="${rest#*/}"
  done
  return 1
}

# ---- what there is ----------------------------------------------------------

data="$(read_env "$DATA_KEY")"
data="${data:-${DATA_DEFAULT:-}}"
[[ -z "$data" || "$data" == /* ]] || data="$HERE/${data#./}"
[[ -d "$data" ]] && data="$(cd "$data" && pwd -P)"

config="$("${compose[@]}" config 2>/dev/null || true)"
project="$(printf '%s\n' "$config" | sed -n 's/^name: //p')"
project="${project%%$'\n'*}"
[[ -n "$project" ]] || project="$(basename "$HERE" | tr '[:upper:]' '[:lower:]')"

containers=() networks=() volumes=() images=() others=() changed=()
while IFS= read -r x; do [[ -n "$x" ]] && containers+=("$x"); done < <(
  "${compose[@]}" ps -a --format '{{.Name}}' 2>/dev/null || true)
while IFS= read -r x; do [[ -n "$x" ]] && networks+=("$x"); done < <(
  docker network ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}' 2>/dev/null || true)
while IFS= read -r x; do [[ -n "$x" ]] && volumes+=("$x"); done < <(
  docker volume ls --filter "label=com.docker.compose.project=$project" --format '{{.Name}}' 2>/dev/null || true)
while IFS= read -r x; do [[ -n "$x" ]] && images+=("$x"); done < <(
  "${compose[@]}" config --images 2>/dev/null | sort -u || true)
while IFS= read -r x; do
  path="${x:3}"
  case "$x" in
    "?? "*|"!! "*) [[ "$path" == "$ENV_FILE" ]] || is_regenerable "$path" || others+=("$path") ;;
    *) changed+=("$path") ;;
  esac
done < <(git status --porcelain --ignored --untracked-files=normal 2>/dev/null || true)

data_before="" size_before=""
if [[ -n "$data" && -d "$data" ]]; then
  data_before="$(top_names "$data")"
  size_before="$(size_of "$data")"
fi

# ---- the plan -----------------------------------------------------------------

echo
echo "== Uninstall: $project  (this folder: $HERE)"
echo
echo "Will be REMOVED -- none of this is your data:"
if (( ${#containers[@]} )); then
  echo "  containers: $(join "${containers[@]}")"
else
  echo "  containers: none (already removed)"
fi
(( ${#networks[@]} )) && echo "  networks:   $(join "${networks[@]}")"
if [[ "$REMOVE_IMAGES" == 1 ]]; then
  (( ${#images[@]} )) && echo "  images:     $(join "${images[@]}")  (downloaded/rebuilt again by make start; one still used by another app is kept)"
else
  (( ${#images[@]} )) && echo "  images:     kept -- REMOVE_IMAGES=1 to delete them too"
fi
echo "  (\`make start\` recreates all of it, with the same data)"
echo
echo "Will be KEPT -- uninstall never deletes these:"
if [[ -z "$data" ]]; then
  echo "  data:       ? -- $DATA_KEY isn't set in $ENV_FILE; nothing is deleted either way"
elif [[ -d "$data" ]]; then
  echo "  data:       $data  (${size_before:-size unreadable}; holds: ${data_before:-nothing yet})"
else
  echo "  data:       $data  (doesn't exist yet -- nothing stored)"
fi
(( ${#volumes[@]} )) && echo "  volumes:    $(join "${volumes[@]}")  (Docker volumes -- data too)"
if [[ -L "$ENV_FILE" ]]; then
  echo "  settings:   $ENV_FILE -> $(readlink "$ENV_FILE")  (the passwords your data was created with)"
elif [[ -f "$ENV_FILE" ]]; then
  echo "  settings:   $HERE/$ENV_FILE  (the passwords your data was created with)"
fi
if (( ${#others[@]} )); then
  echo "  in this folder, not code (left exactly where they are):"
  lines "${others[@]}" | sed 's/^/                /'
fi
if (( ${#changed[@]} )); then
  echo "  code changes not committed (left as they are): $(join "${changed[@]}")"
fi
echo "  code:       this folder. Delete it yourself if you like -- but copy out"
echo "              $ENV_FILE and anything listed above first."
echo

if [[ "$PLAN" == 1 ]]; then
  echo "PLAN=1: nothing was changed."
  exit 0
fi

if [[ "$CONFIRM" != yes ]]; then
  if [[ ! -t 0 ]]; then
    echo "error: not asking without a terminal -- re-run with CONFIRM=yes" >&2
    exit 1
  fi
  read -r -p "Remove what's listed under REMOVED? Your data stays. [y/N] " answer
  [[ "$answer" == [yY]* ]] || { echo "Nothing changed."; exit 1; }
fi

# ---- do it --------------------------------------------------------------------

# Never -v (named volumes are data) and never --rmi here (images are below,
# one at a time, so one shared with another app is kept rather than failing).
"${compose[@]}" down --remove-orphans

if [[ "$REMOVE_IMAGES" == 1 ]]; then
  for img in ${images[@]+"${images[@]}"}; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      docker image rm "$img" >/dev/null 2>&1 && echo "removed image $img" \
        || echo "kept image $img (another container still uses it)"
    fi
  done
fi

# ---- prove the data is still there --------------------------------------------

echo
echo "Data check:"
failed=0
if [[ -n "$data" && -n "$data_before" ]]; then
  if [[ -d "$data" ]] && still_there "$data" "$data_before"; then
    echo "  OK  $data -- still there, $(size_of "$data") (was ${size_before:-?}): $data_before"
  else
    echo "  !!  $data -- NOT as it was. Before: $data_before / now: $(top_names "$data")" >&2
    failed=1
  fi
elif [[ -n "$data" ]]; then
  echo "  OK  $data -- had nothing in it; untouched"
fi
for v in ${volumes[@]+"${volumes[@]}"}; do
  if docker volume inspect "$v" >/dev/null 2>&1; then
    echo "  OK  volume $v -- still there"
  else
    echo "  !!  volume $v -- MISSING" >&2
    failed=1
  fi
done
if [[ -e "$ENV_FILE" ]]; then
  echo "  OK  $ENV_FILE -- still there"
fi
(( failed == 0 )) || { echo "error: something above is not as it was -- stop and look before doing anything else" >&2; exit 1; }

echo
echo "Uninstalled. Nothing of it is running, and it won't come back after a reboot."
echo "To reinstall: make start  (same $ENV_FILE, same data -- picks up where it left off)"
echo "Deleting the data is never done by any make target; that's yours to do by hand."
