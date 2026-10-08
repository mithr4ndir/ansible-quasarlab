#!/usr/bin/env bash
# Force an automation checkout to exactly match a remote ref.
#
# Scheduled runs must apply reviewed, merged code and nothing else. The old
# behaviour was `git pull --ff-only origin main` inside the operator's own
# working tree: when that tree sat on a feature branch the fast-forward failed,
# the return code was never checked, and the run silently applied whatever
# happened to be checked out. On 2026-08-24 that deployed an unmerged branch to
# the whole fleet, including a Jellyfin restart.
#
# This is deliberately destructive (reset --hard + clean -fd). It must only ever
# be pointed at a checkout dedicated to automation, never at a tree a human
# edits.
# Serialised because the two scheduled services share one checkout.
#
# ansible-proxmox and ansible-security both sync /var/lib/ansible-quasarlab/repo.
# When they overlap, the second one to arrive hits:
#
#   fatal: Unable to create '.../.git/index.lock': File exists.
#   FATAL: could not pin ... to origin/main; refusing to run.
#
# Observed on 2026-10-07 02:01:42, when a cmd_center run deployed new timer units
# and its handler restarted both timers, firing both services in the same second.
# The refusal is the guard working as intended (better than running from a
# half-synced tree) but the run is still lost, and the error says nothing about
# the real cause. The fixed OnCalendar slots make routine overlap rare, not
# impossible: a deploy restarts both timers, and TimeoutStartSec is an hour.
#
# The lock is held only for the sync, never for the playbook run, so a long
# proxmox run cannot block the security timer.
sync_repo_to_remote_ref() {
    local dir="$1" ref="$2"

    [[ -d "${dir}/.git" ]] || { echo "sync-repo: ${dir} is not a git checkout" >&2; return 1; }

    local lock="${dir}.sync.lock"
    exec {_sync_lock_fd}>"$lock" || {
        echo "sync-repo: cannot open lock ${lock}" >&2; return 1; }
    # Bounded: a peer sync is seconds of work, so the default 120s means
    # something is wedged, and failing loudly beats blocking the timer until
    # TimeoutStartSec. SYNC_REPO_LOCK_TIMEOUT exists so tests can assert the
    # wait-and-give-up path without sitting there for two minutes.
    if ! flock --timeout "${SYNC_REPO_LOCK_TIMEOUT:-120}" "$_sync_lock_fd"; then
        echo "sync-repo: timed out waiting for another run to finish syncing ${dir}" >&2
        exec {_sync_lock_fd}>&-
        return 1
    fi
    # Release on every return path below, including the failures.
    _sync_unlock() { exec {_sync_lock_fd}>&-; }

    git -C "$dir" fetch --quiet --prune origin || {
        echo "sync-repo: fetch failed for ${dir}" >&2; _sync_unlock; return 1; }
    # --force matters: a plain checkout refuses when the tree has local
    # modifications, which would make a single dirty file wedge every future
    # scheduled run. This checkout is disposable, so discard and move on.
    git -C "$dir" checkout --quiet --force --detach "origin/${ref}" || {
        echo "sync-repo: checkout of origin/${ref} failed for ${dir}" >&2; _sync_unlock; return 1; }
    git -C "$dir" reset --quiet --hard "origin/${ref}" || {
        echo "sync-repo: reset to origin/${ref} failed for ${dir}" >&2; _sync_unlock; return 1; }
    git -C "$dir" clean -qfd || {
        echo "sync-repo: clean failed for ${dir}" >&2; _sync_unlock; return 1; }

    # Prove we ended up where we intended rather than trusting the commands.
    local head remote
    head=$(git -C "$dir" rev-parse HEAD)
    remote=$(git -C "$dir" rev-parse "origin/${ref}")
    if [[ "$head" != "$remote" ]]; then
        echo "sync-repo: ${dir} HEAD ${head} != origin/${ref} ${remote}" >&2
        _sync_unlock
        return 1
    fi
    _sync_unlock
    echo "sync-repo: ${dir} pinned to origin/${ref} @ ${head:0:8}"
}
