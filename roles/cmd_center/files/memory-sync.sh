#!/usr/bin/env bash
# Commit the live memory/ directory to claude-config through a reviewed path.
# Installed by ansible-quasarlab roles/cmd_center (memory_sync.yml) and run by
# the memory-sync.timer user timer on command-center1.
#
# ~/.claude/projects/<cwd>/memory and ~/.claude/memory are symlinks into this
# repo's memory/, so every memory an agent writes lands in the working tree of
# the live checkout. Write-back used to be manual ("commit whenever you
# want"), and the last memory commit was 2026-05-16: by 2026-10-09, 68 memory
# changes existed only on one disk.
#
# Each run:
#   1. copies live memory/ into a dedicated sync worktree based on origin/main,
#      carrying over deletions the live checkout made, never deletions of files
#      another clone added upstream;
#   2. secret-scans the result with gitleaks and refuses to push on any finding;
#   3. commits on a memory-sync/<time> branch, opens a PR, squash-merges it;
#   4. moves the live checkout's HEAD to the new origin/main WITHOUT touching
#      its working tree (git reset --mixed), so uncommitted non-memory work in
#      that checkout is left exactly as it was.
#
# Only memory/ is ever staged. Writes a node_exporter textfile heartbeat.
set -euo pipefail

REPO="${MEMORY_SYNC_REPO:-$HOME/code/claude-config}"
SYNC_WT="${MEMORY_SYNC_WORKTREE:-$HOME/.cache/claude-config-memory-sync}"
PROM="${MEMORY_SYNC_PROM:-/var/lib/node_exporter/textfiles/memory_sync.prom}"
GITLEAKS_IMAGE="${GITLEAKS_IMAGE:-ghcr.io/gitleaks/gitleaks:v8.21.2}"
REMOTE="${MEMORY_SYNC_REMOTE:-origin}"

log() { printf 'memory-sync: %s\n' "$*" >&2; }

files_changed=0
blocked=0
success_ts=""
write_prom() {
    local dir tmp
    dir="$(dirname "$PROM")"
    [ -d "$dir" ] && [ -w "$dir" ] || return 0
    tmp="$(mktemp "$dir/.memory_sync.XXXXXX")" || return 0
    # Keep the last success time across failed runs, so staleness is real.
    if [ -z "$success_ts" ] && [ -r "$PROM" ]; then
        success_ts="$(awk '/^memory_sync_last_success_timestamp_seconds /{print $2}' "$PROM")"
    fi
    {
        echo "# HELP memory_sync_last_run_timestamp_seconds Unix time the memory sync last ran."
        echo "# TYPE memory_sync_last_run_timestamp_seconds gauge"
        echo "memory_sync_last_run_timestamp_seconds $(date +%s)"
        echo "# HELP memory_sync_last_success_timestamp_seconds Unix time memory was last confirmed synced to origin/main."
        echo "# TYPE memory_sync_last_success_timestamp_seconds gauge"
        [ -n "$success_ts" ] && echo "memory_sync_last_success_timestamp_seconds $success_ts"
        echo "# HELP memory_sync_blocked 1 if the last run refused to push because the secret scan found something."
        echo "# TYPE memory_sync_blocked gauge"
        echo "memory_sync_blocked $blocked"
        echo "# HELP memory_sync_files_changed Memory files committed by the last run."
        echo "# TYPE memory_sync_files_changed gauge"
        echo "memory_sync_files_changed $files_changed"
    } > "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$PROM"
}
trap write_prom EXIT

# One run at a time: a second run racing the first would push twice.
mkdir -p "$(dirname "$SYNC_WT")"
exec 9>"${SYNC_WT}.lock"
flock -w 600 9 || { log "another sync holds the lock"; exit 1; }

git -C "$REPO" fetch -q "$REMOTE" main

# Sync worktree, reset to origin/main every run. It is ours alone, so a hard
# reset there cannot lose anyone's work.
if [ ! -e "$SYNC_WT/.git" ]; then
    git -C "$REPO" worktree prune
    git -C "$REPO" worktree add -q --detach "$SYNC_WT" "$REMOTE/main"
else
    git -C "$SYNC_WT" checkout -q --detach "$REMOTE/main"
    git -C "$SYNC_WT" reset -q --hard "$REMOTE/main"
    git -C "$SYNC_WT" clean -q -fd -- memory
fi

# Additions and modifications. No --delete: a file another clone pushed is in
# origin/main but not on this disk, and that is not a deletion. --checksum
# because the default size+mtime check skips a same-size edit made within the
# same second as the checkout above, which the tests hit.
rsync -a --checksum --exclude '.git' "$REPO/memory/" "$SYNC_WT/memory/"

# Real deletions: tracked in the live checkout's HEAD, gone from its disk.
while IFS= read -r -d '' path; do
    if [ -e "$SYNC_WT/$path" ]; then git -C "$SYNC_WT" rm -q -- "$path"; fi
done < <(git -C "$REPO" ls-files -z --deleted -- memory)

git -C "$SYNC_WT" add -A -- memory
files_changed="$(git -C "$SYNC_WT" diff --cached --name-only | wc -l | tr -d " ")"

update_live_checkout() {
    # Move HEAD and index to origin/main, never the working tree. Guarded so it
    # can only fast-forward a clean-index main and can never drop a commit.
    local branch before after restore
    branch="$(git -C "$REPO" symbolic-ref --quiet --short HEAD || true)"
    if [ "$branch" != main ]; then log "live checkout is on '$branch', not moving it"; return 0; fi
    if ! git -C "$REPO" diff --cached --quiet; then log "live checkout has staged changes, not moving it"; return 0; fi
    if ! git -C "$REPO" merge-base --is-ancestor HEAD "$REMOTE/main"; then
        log "live main has commits origin/main lacks, not moving it"; return 0
    fi
    # Upstream changes outside memory/ (merged PRs nobody pulled here) would,
    # after a --mixed reset, make this checkout's older files look like
    # reverts and deletions, which one careless `git add -A` would commit.
    # Pulling those is a person's job; the sync stays correct without it,
    # because deletions are always judged against this checkout's own HEAD.
    if [ -n "$(git -C "$REPO" diff --name-only HEAD "$REMOTE/main" -- . ':(exclude)memory')" ]; then
        log "origin/main has non-memory changes this checkout never pulled; not moving HEAD"; return 0
    fi
    before="$(git -C "$REPO" ls-files --deleted -- memory | sort)"
    git -C "$REPO" reset -q --mixed "$REMOTE/main"
    after="$(git -C "$REPO" ls-files --deleted -- memory | sort)"
    # Files that only look deleted because another clone added them upstream:
    # materialise them, so the next run does not mistake them for deletions.
    restore="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | sed '/^$/d')"
    if [ -n "$restore" ]; then
        printf '%s\n' "$restore" | while IFS= read -r p; do git -C "$REPO" checkout -q -- "$p"; done
    fi
}

if [ "$files_changed" -eq 0 ]; then
    update_live_checkout
    success_ts="$(date +%s)"
    log "nothing to sync"
    exit 0
fi

# Fail closed: no scanner, no push.
if ! docker run --rm --network none -v "$SYNC_WT/memory:/scan:ro" "$GITLEAKS_IMAGE" \
        dir /scan --redact --no-banner --exit-code 3 >/dev/null 2>&1; then
    blocked=1
    log "secret scan failed or found something in memory/; NOT pushing."
    log "inspect with: docker run --rm -v $REPO/memory:/scan:ro $GITLEAKS_IMAGE dir /scan --redact"
    exit 1
fi

stamp="$(date -u +%Y%m%d-%H%M%S)"
branch="memory-sync/$stamp"
summary="$(git -C "$SYNC_WT" diff --cached --name-status | sed 's/^/    /')"
git -C "$SYNC_WT" -c user.name="$(git -C "$REPO" config user.name)" \
    -c user.email="$(git -C "$REPO" config user.email)" \
    commit -q -m "memory: sync $files_changed file(s), $stamp" -m "Automated by bin/memory-sync.sh. Secret scan (gitleaks, redacted) passed.

$summary"
git -C "$SYNC_WT" push -q "$REMOTE" "HEAD:refs/heads/$branch"
(
    cd "$SYNC_WT"
    gh pr create --base main --head "$branch" --title "memory: sync $files_changed file(s), $stamp" \
        --body "Automated memory write-back from \`bin/memory-sync.sh\`. Secret scan (gitleaks, redacted) passed. Files:

\`\`\`
$summary
\`\`\`" >/dev/null
    gh pr merge "$branch" --squash --delete-branch >/dev/null
)

git -C "$REPO" fetch -q "$REMOTE" main
update_live_checkout
success_ts="$(date +%s)"
log "synced $files_changed file(s) via $branch"
