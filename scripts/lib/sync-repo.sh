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
sync_repo_to_remote_ref() {
    local dir="$1" ref="$2"

    [[ -d "${dir}/.git" ]] || { echo "sync-repo: ${dir} is not a git checkout" >&2; return 1; }

    git -C "$dir" fetch --quiet --prune origin || {
        echo "sync-repo: fetch failed for ${dir}" >&2; return 1; }
    # --force matters: a plain checkout refuses when the tree has local
    # modifications, which would make a single dirty file wedge every future
    # scheduled run. This checkout is disposable, so discard and move on.
    git -C "$dir" checkout --quiet --force --detach "origin/${ref}" || {
        echo "sync-repo: checkout of origin/${ref} failed for ${dir}" >&2; return 1; }
    git -C "$dir" reset --quiet --hard "origin/${ref}" || {
        echo "sync-repo: reset to origin/${ref} failed for ${dir}" >&2; return 1; }
    git -C "$dir" clean -qfd || {
        echo "sync-repo: clean failed for ${dir}" >&2; return 1; }

    # Prove we ended up where we intended rather than trusting the commands.
    local head remote
    head=$(git -C "$dir" rev-parse HEAD)
    remote=$(git -C "$dir" rev-parse "origin/${ref}")
    if [[ "$head" != "$remote" ]]; then
        echo "sync-repo: ${dir} HEAD ${head} != origin/${ref} ${remote}" >&2
        return 1
    fi
    echo "sync-repo: ${dir} pinned to origin/${ref} @ ${head:0:8}"
}
