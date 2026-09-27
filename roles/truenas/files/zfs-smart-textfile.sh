#!/usr/bin/env bash
# Emits ZFS pool + SMART metrics for node_exporter's textfile collector.
#
# node_exporter's built-in ZFS collector covers ARC and per-dataset I/O but has
# no pool capacity, no pool health, and no SMART. On TrueNAS the root filesystem
# is read-only and Vector cannot be installed, but node_exporter already runs
# here with --collector.textfile.directory, so this needs no new service.
#
# Written atomically: node_exporter reads the directory on every scrape and a
# half-written file would surface as a parse error.
set -uo pipefail

# Must match the directory node_exporter was started with. The role passes
# truenas_textfile_dir through the service environment; the default keeps a
# manual invocation working.
OUT_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfiles}"
OUT="${OUT_DIR}/zfs_smart.prom"
TMP="$(mktemp "${OUT_DIR}/.zfs_smart.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

health_code() {
    case "$1" in
        ONLINE) echo 0 ;; DEGRADED) echo 1 ;; FAULTED) echo 2 ;;
        OFFLINE) echo 3 ;; UNAVAIL) echo 4 ;; REMOVED) echo 5 ;; *) echo 6 ;;
    esac
}

{
    echo "# HELP zfs_pool_size_bytes Total pool size."
    echo "# TYPE zfs_pool_size_bytes gauge"
    echo "# HELP zfs_pool_allocated_bytes Allocated space."
    echo "# TYPE zfs_pool_allocated_bytes gauge"
    echo "# HELP zfs_pool_free_bytes Free space."
    echo "# TYPE zfs_pool_free_bytes gauge"
    echo "# HELP zfs_pool_health 0=ONLINE 1=DEGRADED 2=FAULTED 3=OFFLINE 4=UNAVAIL 5=REMOVED 6=UNKNOWN."
    echo "# TYPE zfs_pool_health gauge"
    echo "# HELP zfs_pool_fragmentation_ratio Fragmentation as a ratio."
    echo "# TYPE zfs_pool_fragmentation_ratio gauge"
    echo "# HELP zfs_pool_capacity_ratio Used capacity as a ratio."
    echo "# TYPE zfs_pool_capacity_ratio gauge"

    while IFS=$'\t' read -r name size alloc free health frag cap; do
        [ -z "${name:-}" ] && continue
        echo "zfs_pool_size_bytes{pool=\"$name\"} $size"
        echo "zfs_pool_allocated_bytes{pool=\"$name\"} $alloc"
        echo "zfs_pool_free_bytes{pool=\"$name\"} $free"
        echo "zfs_pool_health{pool=\"$name\"} $(health_code "$health")"
        echo "zfs_pool_fragmentation_ratio{pool=\"$name\"} $(awk -v v="${frag:-0}" 'BEGIN{printf "%.4f", v/100}')"
        echo "zfs_pool_capacity_ratio{pool=\"$name\"} $(awk -v v="${cap:-0}" 'BEGIN{printf "%.4f", v/100}')"
    done < <(zpool list -Hp -o name,size,alloc,free,health,fragmentation,capacity 2>/dev/null)

    echo "# HELP smart_device_health smartctl overall-health: 1=PASSED 0=FAILED."
    echo "# TYPE smart_device_health gauge"
    echo "# HELP smart_power_on_hours Power on hours."
    echo "# TYPE smart_power_on_hours counter"
    echo "# HELP smart_crc_error_count UDMA CRC error count."
    echo "# TYPE smart_crc_error_count counter"
    echo "# HELP smart_reallocated_sector_count Reallocated sector count."
    echo "# TYPE smart_reallocated_sector_count counter"
    echo "# HELP smart_temperature_celsius Drive temperature."
    echo "# TYPE smart_temperature_celsius gauge"
    echo "# HELP smart_enabled Whether SMART is enabled on the device."
    echo "# TYPE smart_enabled gauge"
    echo "# HELP smart_collect_ok Whether smartctl returned usable output for the device."
    echo "# TYPE smart_collect_ok gauge"
    echo "# HELP smart_smartctl_exit_status Raw smartctl exit bitmask; bit 3 set means DISK FAILING."
    echo "# TYPE smart_smartctl_exit_status gauge"

    # Overridable so the failing-disk path can be exercised in tests against a
    # stub smartctl; production always uses the default.
    for dev in ${SMART_DEV_GLOB:-/dev/sd?}; do
        [ -e "$dev" ] || continue
        d="${dev##*/}"

        # smartctl's exit status is a BITMASK, not pass/fail. A disk that fails
        # its overall-health check sets bit 3 and still prints the health and
        # attribute data. The old `|| continue` here therefore skipped exactly
        # the failing disks this exporter exists to catch, which also left the
        # `smart_device_health 0` branch below unreachable: the metric could
        # only ever report 1 or nothing at all.
        #
        #   bit 0 (1)   command line did not parse
        #   bit 1 (2)   device open failed
        #   bit 2 (4)   some SMART or ATA command to the disk failed
        #   bit 3 (8)   SMART status check returned DISK FAILING
        #   bit 4 (16)  prefail attributes <= threshold
        #   bit 5 (32)  some attribute was <= threshold in the past
        #   bit 6 (64)  error log contains errors
        #   bit 7 (128) self-test log contains errors
        #
        # Only bits 0-1 mean we never reached the device and have nothing
        # trustworthy to parse. Every other bit is a health signal to keep.
        info="$(smartctl -i -A -H "$dev" 2>/dev/null)"
        rc=$?

        if [ $(( rc & 3 )) -ne 0 ] || [ -z "$info" ]; then
            # Say so explicitly rather than vanishing from the output: a disk
            # that silently stops being reported looks identical to a healthy
            # one on a dashboard.
            echo "smart_collect_ok{device=\"$d\"} 0"
            continue
        fi
        echo "smart_collect_ok{device=\"$d\"} 1"
        echo "smart_smartctl_exit_status{device=\"$d\"} $rc"
        model="$(printf '%s' "$info" | awk -F: '/Device Model/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}')"
        serial="$(printf '%s' "$info" | awk -F: '/Serial Number/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}')"
        sup="$(printf '%s' "$info" | awk -F: '/SMART support is/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' | tail -1)"
        lbl="device=\"$d\",model=\"${model:-unknown}\",serial=\"${serial:-unknown}\""

        case "$sup" in Enabled) echo "smart_enabled{$lbl} 1" ;; *) echo "smart_enabled{$lbl} 0" ;; esac

        case "$(printf '%s' "$info" | awk -F: '/overall-health/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}')" in
            PASSED) echo "smart_device_health{$lbl} 1" ;;
            "")     : ;;
            *)      echo "smart_device_health{$lbl} 0" ;;
        esac

        poh="$(printf '%s' "$info" | awk '/Power_On_Hours/{print $10; exit}')"
        crc="$(printf '%s' "$info" | awk '/CRC_Error_Count/{print $10; exit}')"
        ral="$(printf '%s' "$info" | awk '/Reallocated_Sector/{print $10; exit}')"
        tmp="$(printf '%s' "$info" | awk '/Temperature_Celsius|Airflow_Temperature/{print $10; exit}')"
        [ -n "${poh:-}" ] && echo "smart_power_on_hours{$lbl} $poh"
        [ -n "${crc:-}" ] && echo "smart_crc_error_count{$lbl} $crc"
        [ -n "${ral:-}" ] && echo "smart_reallocated_sector_count{$lbl} $ral"
        [ -n "${tmp:-}" ] && echo "smart_temperature_celsius{$lbl} $tmp"
    done

    echo "# HELP zfs_smart_textfile_last_run_timestamp_seconds Unix time of the last successful run."
    echo "# TYPE zfs_smart_textfile_last_run_timestamp_seconds gauge"
    echo "zfs_smart_textfile_last_run_timestamp_seconds $(date +%s)"
} > "$TMP"

chmod 0644 "$TMP"
mv "$TMP" "$OUT"
trap - EXIT
