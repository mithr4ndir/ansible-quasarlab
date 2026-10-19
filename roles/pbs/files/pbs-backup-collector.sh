#!/bin/bash
# PBS backup collector for the node_exporter textfile collector.
#
# Prometheus has 295 alert rules and not one of them mentions backups, so
# until this exists a silent backup failure is only discovered during a
# restore. That is the same shape as the Wazuh outage (every service read
# "active" for four months) and the vzdump jobs that ran green against disks
# excluded by backup=0.
#
# WHY READ THE DATASTORE AND NOT THE API
# The PBS API needs a token, and a token is another secret to provision,
# rotate and have expire quietly. The on-disk layout carries everything we
# need and needs no auth:
#
#   /mnt/datastore/<store>/<type>/<guest>/<RFC3339 timestamp>Z/
#       index.json.blob        written LAST, so its presence means the
#                              backup completed rather than aborted midway
#       *.fidx / *.didx        one per archived disk
#       client.log.blob
#
# So "newest directory" is NOT the signal. A partial snapshot leaves a
# directory behind with no manifest. This collector only considers a
# snapshot that has a manifest, and separately counts disk indexes, because
# a manifest with zero disks is the PBS version of the backup=0 trap: a
# backup that exists, reports success, and restores nothing.
#
# COVERAGE GAP, stated plainly
# If this collector stops running, the .prom file stays on disk and
# node_exporter keeps serving the last values. The snapshot timestamps then
# stop advancing, so PbsBackupStale fires on its own. If the file is removed
# entirely the series vanish, which is why the alert rules pair every
# threshold with an absent() check. The collector's own heartbeat is
# pbs_backup_collector_timestamp_seconds.
#
# Guest IDs become label values, so they are matched against a strict
# pattern and anything else is skipped rather than written out.
set -uo pipefail

DATASTORE_ROOT="${PBS_DATASTORE_ROOT:-/mnt/datastore}"
PROM_FILE="${PBS_COLLECTOR_PROM_FILE:-/var/lib/node_exporter/textfiles/pbs_backup.prom}"
LOG_TAG="pbs-backup-collector"

tmp="$(mktemp "${PROM_FILE}.XXXXXX")" || exit 1
trap 'rm -f "$tmp"' EXIT

# errexit is deliberately off, because an unreadable snapshot directory should
# cost one guest's series rather than the whole file. That makes every write
# into $tmp something we have to check by hand: a full filesystem would
# otherwise leave a TRUNCATED file, rename it over the last good metrics, and
# exit 0. Partial metrics are worse than stale ones, because the series that
# failed to write simply vanish and read as "this guest has no backup" or, for
# the heartbeat, as a collector that is still running fine.
write_failed=0
note_write_failure() {
    write_failed=1
}

{
    echo "# HELP pbs_snapshot_last_complete_timestamp_seconds Unix time of the newest snapshot that has a manifest."
    echo "# TYPE pbs_snapshot_last_complete_timestamp_seconds gauge"
    echo "# HELP pbs_snapshot_disk_index_count Disk index files in that newest complete snapshot. Zero means it restores nothing."
    echo "# TYPE pbs_snapshot_disk_index_count gauge"
    echo "# HELP pbs_guest_snapshot_dir_count Snapshot directories for this guest, complete or not."
    echo "# TYPE pbs_guest_snapshot_dir_count gauge"
} >"$tmp" || note_write_failure

guests_total=0
guests_without_complete=0

for store_path in "$DATASTORE_ROOT"/*; do
    [ -d "$store_path" ] || continue
    store="$(basename "$store_path")"
    # Datastore name lands in a label.
    [[ "$store" =~ ^[A-Za-z0-9._-]+$ ]] || continue

    for type_path in "$store_path"/*; do
        [ -d "$type_path" ] || continue
        gtype="$(basename "$type_path")"
        # PBS uses vm, ct and host. lost+found is not a backup type.
        case "$gtype" in
            vm | ct | host) ;;
            *) continue ;;
        esac

        for guest_path in "$type_path"/*; do
            [ -d "$guest_path" ] || continue
            guest="$(basename "$guest_path")"
            [[ "$guest" =~ ^[A-Za-z0-9._-]+$ ]] || continue

            guests_total=$((guests_total + 1))

            # Newest snapshot that actually completed. Directory names are
            # RFC3339 in UTC, so a reverse lexical sort is a reverse time
            # sort, and we can stop at the first one with a manifest.
            newest_epoch=""
            disk_count=0
            snap_dirs=0
            while IFS= read -r snap; do
                [ -n "$snap" ] || continue
                snap_dirs=$((snap_dirs + 1))
                [ -f "$guest_path/$snap/index.json.blob" ] || continue
                # %Y-%m-%dT%H:%M:%SZ -> epoch. Reject anything date cannot parse.
                if ! epoch="$(date -u -d "$snap" +%s 2>/dev/null)"; then
                    continue
                fi
                # Reverse lexical order is reverse time order, so the first
                # snapshot with a manifest is the newest complete one. The
                # walk continues anyway, because snap_dirs has to count every
                # directory and not just the ones down to the newest good one.
                if [ -z "$newest_epoch" ]; then
                    newest_epoch="$epoch"
                    disk_count="$(find "$guest_path/$snap" -maxdepth 1 -type f \
                        \( -name '*.fidx' -o -name '*.didx' \) 2>/dev/null | wc -l)"
                fi
            done < <(ls -1 "$guest_path" 2>/dev/null | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' | sort -r)

            # Emitted for every guest, including those with nothing at all, so
            # a guest registered in the datastore with zero attempts recorded
            # is VISIBLE (dir_count 0) without being alertable. pbs1 itself
            # (vm103) is permanently in that state: the hourly job excludes
            # it, and a guest whose backups fail before PBS creates a snapshot
            # directory leaves no other trace here. An alert on "registered
            # but never completed" would therefore be permanently red, which
            # is worse than no alert. The alertable case is dir_count > 0 with
            # no complete snapshot: attempts happened and every one failed.
            printf 'pbs_guest_snapshot_dir_count{datastore="%s",guest_type="%s",guest="%s"} %s\n' \
                "$store" "$gtype" "$guest" "$snap_dirs" >>"$tmp" || note_write_failure

            if [ -z "$newest_epoch" ]; then
                guests_without_complete=$((guests_without_complete + 1))
                continue
            fi

            printf 'pbs_snapshot_last_complete_timestamp_seconds{datastore="%s",guest_type="%s",guest="%s"} %s\n' \
                "$store" "$gtype" "$guest" "$newest_epoch" >>"$tmp" || note_write_failure
            printf 'pbs_snapshot_disk_index_count{datastore="%s",guest_type="%s",guest="%s"} %s\n' \
                "$store" "$gtype" "$guest" "$disk_count" >>"$tmp" || note_write_failure
        done
    done
done

{
    echo "# HELP pbs_backup_collector_guests_total Guests with at least one snapshot directory."
    echo "# TYPE pbs_backup_collector_guests_total gauge"
    echo "pbs_backup_collector_guests_total ${guests_total}"
    echo "# HELP pbs_backup_collector_guests_without_complete Guests whose snapshots all lack a manifest."
    echo "# TYPE pbs_backup_collector_guests_without_complete gauge"
    echo "pbs_backup_collector_guests_without_complete ${guests_without_complete}"
    echo "# HELP pbs_backup_collector_timestamp_seconds Unix time this collector last completed."
    echo "# TYPE pbs_backup_collector_timestamp_seconds gauge"
    echo "pbs_backup_collector_timestamp_seconds $(date +%s)"
} >>"$tmp" || note_write_failure

# PUBLISH, or refuse to. The heartbeat is the LAST line written, so its
# presence is what proves the file is whole: the same trick PBS itself uses
# with index.json.blob, and the reason this collector trusts a manifest over a
# directory. Check it in addition to the write flag, because a short write can
# return success on a buffered stream and only fail on close.
if [ "$write_failed" -ne 0 ] \
    || ! grep -qE '^pbs_backup_collector_timestamp_seconds [0-9]+$' "$tmp" \
    || ! chmod 0644 "$tmp"; then
    logger -t "$LOG_TAG" \
        "reason=incomplete_metrics action=kept_previous path=$PROM_FILE"
    exit 1
fi

# node_exporter reads this directory continuously, so swap it in atomically
# rather than letting it observe a half-written file.
if ! mv -f "$tmp" "$PROM_FILE"; then
    logger -t "$LOG_TAG" "reason=write_failed path=$PROM_FILE"
    exit 1
fi
trap - EXIT

logger -t "$LOG_TAG" "guests=$guests_total without_complete=$guests_without_complete"
exit 0
