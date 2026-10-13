#!/usr/bin/env bash
# Runs check-prose.py on what a PostToolUse event wrote: the one file named by
# `tool_input.file_path`, or the files a `tool_input.command` changed, so a heredoc or a
# `sed -i` gets the same checks as the edit tools.
set -uo pipefail

: "${CLAUDE_PROJECT_DIR:=$PWD}"

payload=$(cat)
read_field() {
  printf '%s' "$payload" | python3 -c "import json,sys; print(json.load(sys.stdin).get('tool_input',{}).get('$1',''))" 2>/dev/null
}

# Three layouts: a plugin install, a copy install, and the kit checked out as itself.
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "$CLAUDE_PLUGIN_ROOT/voice/register.txt" ]; then
  kit_root="$CLAUDE_PLUGIN_ROOT"
elif [ -f "$CLAUDE_PROJECT_DIR/.claude/voice/register.txt" ]; then
  kit_root="$CLAUDE_PROJECT_DIR/.claude"
else
  kit_root="$CLAUDE_PROJECT_DIR"
fi
register="$kit_root/voice/register.txt"
checker="$kit_root/hooks/check-prose.py"
local_register="$CLAUDE_PROJECT_DIR/.claude/register.local.txt"
local_patterns="$CLAUDE_PROJECT_DIR/.claude/style-patterns.local.txt"
exempt_list="$CLAUDE_PROJECT_DIR/.claude/style-exempt.txt"

[ -f "$checker" ] || exit 0

exempt() {
  case "$1" in
    */third_party/*|*/vendor/*|*/sdk/*|*/node_modules/*|*/.git/*|*/dist/*|*/build/*) return 0 ;;
    *.local.md|*.local.txt|*/_todo/*|*/_done/*|*/_tmp/*|*/.claude/*) return 0 ;;
    *.lock|*.snap|*.min.js|*.min.css) return 0 ;;
  esac
  if [ -f "$exempt_list" ]; then
    while IFS= read -r glob; do
      case "$glob" in ""|\#*) continue ;; esac
      # shellcheck disable=SC2254  # the pattern is the point; it must stay unquoted
      case "$1" in $glob) return 0 ;; esac
    done < "$exempt_list"
  fi
  return 1
}

problems=()

check_file() {
  local label="${1#"$CLAUDE_PROJECT_DIR/"}" found
  found=$(python3 "$checker" "$1" "$register" "$local_register" "$local_patterns" 2>/dev/null)
  if [ -n "$found" ]; then
    while IFS= read -r entry; do problems+=("$label: $entry"); done <<< "$found"
  fi
}

file=$(read_field file_path)

if [ -n "$file" ]; then
  { [ -f "$file" ] && ! exempt "$file"; } || exit 0
  check_file "$file"
else
  command=$(read_field command)
  [ -n "$command" ] || exit 0
  # Only commands that could have written a file are worth the scan below.
  printf '%s' "$command" | grep -qE '(>|>>|<<|\btee\b|\bsed\b[^|]*-i|\bcp\b|\bmv\b|\binstall\b|\bpatch\b|\bdd\b|\btruncate\b|git +apply|\bpython3?\b|\bnode\b|\bperl\b|\b(pnpm|npm|npx|yarn|biome|prettier)\b)' || exit 0

  git_dir=$(git -C "$CLAUDE_PROJECT_DIR" rev-parse --git-dir 2>/dev/null) || exit 0
  case "$git_dir" in /*) ;; *) git_dir="$CLAUDE_PROJECT_DIR/$git_dir" ;; esac
  # A new snapshot name takes the record-only path below, so a format change never
  # reports the whole window as changed.
  snapshot="$git_dir/style-check-snapshot-v2"

  # Checksums rather than timestamps, because a write within the same second as the last
  # scan is the normal case. The scan is capped at the most recently written files that
  # still exist, so a pile of deletions cannot push the triggering write out of the window.
  current=$(python3 - "$CLAUDE_PROJECT_DIR" <<'PY' 2>/dev/null
import os, stat, subprocess, sys, zlib

BUDGET = 200
root = sys.argv[1]

try:
    porcelain = subprocess.run(
        ['git', '-C', root, 'status', '--porcelain', '--untracked-files=all', '-z'],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
except (OSError, subprocess.CalledProcessError):
    sys.exit(0)

# -z rather than the quoted default, so a path with a space or an accent in it arrives
# intact. Records are NUL-separated; a rename or a copy is followed by a second record
# holding its source path, which is not a status line and must not be read as one.
records = porcelain.split(b'\0')
paths, i = [], 0
while i < len(records):
    record, i = records[i], i + 1
    if len(record) < 4:
        continue
    status, path = record[:2], record[3:]
    if b'R' in status or b'C' in status:
        i += 1
    paths.append(os.fsdecode(path))

entries = []
for path in paths:
    if '\n' in path:  # would corrupt the one-line-per-file snapshot below
        continue
    try:
        info = os.stat(os.path.join(root, path))
    except OSError:  # a deletion, or a symlink pointing at one
        continue
    if stat.S_ISREG(info.st_mode):
        entries.append((info.st_mtime, path))
entries.sort(key=lambda entry: (-entry[0], entry[1]))

for _, path in entries[:BUDGET]:
    checksum = 0
    try:
        with open(os.path.join(root, path), 'rb') as handle:
            for chunk in iter(lambda: handle.read(65536), b''):
                checksum = zlib.crc32(chunk, checksum)
    except OSError:
        continue
    print(checksum, path)
PY
  )

  # First run in a checkout only records the snapshot. Reporting every dirty file at
  # that point would dump a backlog nobody asked for.
  if [ ! -f "$snapshot" ]; then
    printf '%s\n' "$current" > "$snapshot"
    exit 0
  fi

  checked=0
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    grep -qxF "$entry" "$snapshot" && continue
    path=${entry#* }
    full="$CLAUDE_PROJECT_DIR/$path"
    [ -f "$full" ] || continue
    exempt "$full" && continue
    check_file "$full"
    checked=$((checked + 1))
    [ "$checked" -ge 10 ] && break
  done <<< "$current"
  printf '%s\n' "$current" > "$snapshot"
fi

[ ${#problems[@]} -eq 0 ] && exit 0

{
  echo "Style check:"
  for p in "${problems[@]}"; do echo "  - $p"; done
  echo "See voice/constructions.md and doctrine/documentation.md. Fix it now. A line that must quote a banned phrase gets a \`-regex\` allow line in .claude/style-patterns.local.txt."
} >&2
exit 2
