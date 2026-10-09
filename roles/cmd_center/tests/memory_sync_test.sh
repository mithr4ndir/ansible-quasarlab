#!/usr/bin/env bash
# Tests for bin/memory-sync.sh against real git: a bare "GitHub", the live
# checkout, and a second clone standing in for another machine. gh is stubbed
# to merge into the bare repo; docker is stubbed to pass or fail the scan.
set -uo pipefail
SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/files/memory-sync.sh"
pass=0; fail=0
ok()   { echo "PASS: $1"; pass=$((pass + 1)); }
bad()  { echo "FAIL: $1"; fail=$((fail + 1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

setup() {
    T="$(mktemp -d)"
    git init -q --bare -b main "$T/remote.git"
    git clone -q "$T/remote.git" "$T/live" 2>/dev/null
    git -C "$T/live" config user.name t; git -C "$T/live" config user.email t@t
    mkdir -p "$T/live/memory" "$T/live/bin"
    echo "old fact" > "$T/live/memory/a.md"
    echo "keep me" > "$T/live/memory/b.md"
    echo "script v1" > "$T/live/bin/tool.sh"
    git -C "$T/live" add -A; git -C "$T/live" commit -qm init; git -C "$T/live" push -q origin main
    mkdir -p "$T/bin" "$T/prom"
    # gh: `pr create` records, `pr merge <branch>` fast-forwards main to it.
    cat > "$T/bin/gh" <<EOF
#!/bin/sh
echo "gh \$*" >> "$T/gh.log"
if [ "\$1 \$2" = "pr merge" ]; then
  git --git-dir="$T/remote.git" update-ref refs/heads/main "refs/heads/\$3"
  git --git-dir="$T/remote.git" update-ref -d "refs/heads/\$3"
fi
EOF
    cat > "$T/bin/docker" <<EOF
#!/bin/sh
echo "docker \$*" >> "$T/docker.log"
exit \${STUB_SCAN_RC:-0}
EOF
    chmod +x "$T/bin/gh" "$T/bin/docker"
    export PATH="$T/bin:$PATH" MEMORY_SYNC_REPO="$T/live" MEMORY_SYNC_WORKTREE="$T/sync" \
           MEMORY_SYNC_PROM="$T/prom/memory_sync.prom"
}
run() { bash "$SCRIPT" >"$T/out.log" 2>&1; }
remote_file() { git --git-dir="$T/remote.git" show "main:$1" 2>/dev/null; }

# --- 1. a change is committed, merged, and the live checkout ends clean -------
setup
echo "new fact" > "$T/live/memory/a.md"
echo "brand new" > "$T/live/memory/c.md"
run; rc=$?
check "changed memory run exits 0" '[ $rc -eq 0 ]'
check "modified file reached origin/main" '[ "$(remote_file memory/a.md)" = "new fact" ]'
check "new file reached origin/main" '[ "$(remote_file memory/c.md)" = "brand new" ]'
check "went through a PR, not a direct push" 'grep -q "pr create" "$T/gh.log" && grep -q "pr merge memory-sync/" "$T/gh.log"'
check "live checkout shows memory clean afterwards" '[ -z "$(git -C "$T/live" status --porcelain -- memory)" ]'
check "heartbeat records success and 2 files" 'grep -q "^memory_sync_last_success_timestamp_seconds " "$T/prom/memory_sync.prom" && grep -q "^memory_sync_files_changed 2$" "$T/prom/memory_sync.prom"'

# --- 2. uncommitted NON-memory work is never committed or disturbed -----------
setup
echo "script v2, uncommitted rollout work" > "$T/live/bin/tool.sh"
echo "untracked notes" > "$T/live/NOTES.md"
echo "fact 2" > "$T/live/memory/a.md"
run
check "non-memory edit not pushed" '[ "$(remote_file bin/tool.sh)" = "script v1" ]'
check "untracked non-memory file not pushed" '! remote_file NOTES.md >/dev/null'
check "non-memory edit still on disk, still uncommitted" '[ "$(cat "$T/live/bin/tool.sh")" = "script v2, uncommitted rollout work" ] && git -C "$T/live" status --porcelain | grep -q " M bin/tool.sh"'
check "untracked file still on disk" '[ -f "$T/live/NOTES.md" ]'

# --- 3. a secret blocks the push entirely --------------------------------------
setup
echo "leaky" > "$T/live/memory/a.md"
before="$(git --git-dir="$T/remote.git" rev-parse main)"
STUB_SCAN_RC=3 run; rc=$?
check "secret finding exits non-zero" '[ $rc -ne 0 ]'
check "origin/main untouched when blocked" '[ "$(git --git-dir="$T/remote.git" rev-parse main)" = "$before" ]'
check "no branch pushed when blocked" '[ -z "$(git --git-dir="$T/remote.git" for-each-ref refs/heads/memory-sync)" ]'
check "no PR opened when blocked" '[ ! -s "$T/gh.log" ]'
check "heartbeat says blocked" 'grep -q "^memory_sync_blocked 1$" "$T/prom/memory_sync.prom"'
check "local change preserved when blocked" '[ "$(cat "$T/live/memory/a.md")" = "leaky" ]'

# --- 4. scanner unavailable means no push (fail closed) ------------------------
# 127 is what the shell returns for a missing docker binary. Removing the stub
# is not enough: the test host has a real docker further down PATH.
setup
echo "x" > "$T/live/memory/a.md"
before="$(git --git-dir="$T/remote.git" rev-parse main)"
STUB_SCAN_RC=127 run; rc=$?
check "unavailable scanner fails closed" '[ $rc -ne 0 ] && [ "$(git --git-dir="$T/remote.git" rev-parse main)" = "$before" ]'

# --- 5. deletions in the live checkout propagate -------------------------------
setup
rm "$T/live/memory/b.md"
run
check "deleted memory removed from origin/main" '! remote_file memory/b.md >/dev/null'

# --- 6. a file another clone added upstream is NOT deleted ---------------------
setup
git clone -q "$T/remote.git" "$T/other" 2>/dev/null
git -C "$T/other" config user.name o; git -C "$T/other" config user.email o@o
echo "from the other machine" > "$T/other/memory/other.md"
git -C "$T/other" add -A; git -C "$T/other" commit -qm other; git -C "$T/other" push -q origin main
echo "local change" > "$T/live/memory/a.md"
run
check "upstream-added file survives the sync" '[ "$(remote_file memory/other.md)" = "from the other machine" ]'
check "upstream-added file materialised in live checkout" '[ -f "$T/live/memory/other.md" ]'
: > "$T/gh.log"
run
check "second run sees nothing to do (no false deletion)" '[ ! -s "$T/gh.log" ] && remote_file memory/other.md >/dev/null'

# --- 7. nothing changed: no commit, no PR, heartbeat still fresh ---------------
setup
before="$(git --git-dir="$T/remote.git" rev-parse main)"
run; rc=$?
check "no-op run exits 0 without a PR" '[ $rc -eq 0 ] && [ ! -s "$T/gh.log" ] && [ "$(git --git-dir="$T/remote.git" rev-parse main)" = "$before" ]'
check "no-op run still records success" 'grep -q "^memory_sync_last_success_timestamp_seconds " "$T/prom/memory_sync.prom"'

# --- 8. live main with its own unpushed commit is never moved ------------------
setup
echo "local commit" > "$T/live/bin/tool.sh"; git -C "$T/live" commit -qam "unpushed"
head="$(git -C "$T/live" rev-parse HEAD)"
echo "y" > "$T/live/memory/a.md"
run
check "unpushed local commit kept on live main" '[ "$(git -C "$T/live" rev-parse HEAD)" = "$head" ]'

# --- 9. upstream NON-memory changes: live HEAD stays put, no phantom reverts --
setup
git clone -q "$T/remote.git" "$T/other" 2>/dev/null
git -C "$T/other" config user.name o; git -C "$T/other" config user.email o@o
echo "script v2 merged elsewhere" > "$T/other/bin/tool.sh"
echo "new unit" > "$T/other/bin/new-unit.service"
git -C "$T/other" add -A; git -C "$T/other" commit -qm upstream; git -C "$T/other" push -q origin main
head="$(git -C "$T/live" rev-parse HEAD)"
echo "z" > "$T/live/memory/a.md"
run; rc=$?
check "sync still succeeds with unpulled upstream changes" '[ $rc -eq 0 ] && [ "$(remote_file memory/a.md)" = "z" ]'
check "live HEAD not moved past non-memory upstream changes" '[ "$(git -C "$T/live" rev-parse HEAD)" = "$head" ]'
check "no phantom revert or deletion in live status" '! git -C "$T/live" status --porcelain | grep -qE "bin/(tool.sh|new-unit)"'
check "upstream non-memory change kept on origin/main" '[ "$(remote_file bin/tool.sh)" = "script v2 merged elsewhere" ]'
: > "$T/gh.log"
run
check "next run does not re-push the same memory change" '[ ! -s "$T/gh.log" ]'

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
