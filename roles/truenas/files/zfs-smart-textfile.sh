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

# Must match the directory node_exporter was started with. The role defines
# truenas_textfile_dir and creates it, but this file is copied verbatim from
# files/ so it cannot template the value; the service passes it through the
# environment. Without this, overriding the documented setting creates a
# directory nobody writes to and metrics nobody scrapes.
OUT_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfiles}"
OUT="${OUT_DIR}/zfs_smart.prom"
TMP="$(mktemp "${OUT_DIR}/.zfs_smart.XXXXXX")"
trap 'rm -f "$TMP" "${NEW_CACHE:-}"' EXIT

health_code() {
    case "$1" in
        ONLINE) echo 0 ;; DEGRADED) echo 1 ;; FAULTED) echo 2 ;;
        OFFLINE) echo 3 ;; UNAVAIL) echo 4 ;; REMOVED) echo 5 ;; *) echo 6 ;;
    esac
}

# Where a block device lives physically. Overridable so tests can drive a fake
# sysfs; without that a test would read the real disks of whatever host runs it.
SYS_CLASS_BLOCK="${SYS_CLASS_BLOCK:-/sys/class/block}"

# Bay number for a whole-disk name such as sdd. On the UGREEN DXP8800 Plus the
# Nth tray from the left is kernel port ataN; that mapping was verified by LED
# and by a live reseat on 2026-10-08 (quasarlab-disaster-recovery
# architecture/storage.md). The sd letter is NOT stable across boots, which is
# why alerts that only said "sdd" were not enough to pull the right tray.
disk_bay() {
    local port
    case "$1" in nvme*) echo m2; return ;; esac
    port="$(readlink -f "$SYS_CLASS_BLOCK/$1" 2>/dev/null | grep -o 'ata[0-9]*' | head -1)"
    [ -n "$port" ] && echo "${port#ata}" || echo unknown
}

# Serial from the VPD page 80 the kernel cached when the disk attached. Reading
# it sends no I/O to the drive, so it still answers when the drive itself has
# stopped responding, which is exactly when smartctl cannot give us a serial.
# The first four bytes are the VPD header, not part of the serial. NVMe has no
# VPD page; its controller exposes the serial as a plain attribute instead.
disk_serial() {
    local s
    if [ -r "$SYS_CLASS_BLOCK/$1/device/vpd_pg80" ]; then
        s="$(tail -c +5 "$SYS_CLASS_BLOCK/$1/device/vpd_pg80" 2>/dev/null | tr -dc '[:alnum:]_.-')"
    else
        s="$(tr -dc '[:alnum:]_.-' < "$SYS_CLASS_BLOCK/$1/device/serial" 2>/dev/null)"
    fi
    [ -n "$s" ] && echo "$s" || echo unknown
}

# Whole-disk name for a partition path such as /dev/disk/by-partuuid/<uuid>.
# In sysfs a partition is a child directory of its disk, which works for both
# sdd1 and nvme0n1p1 without guessing at naming rules.
disk_of_path() {
    local part
    part="$(basename "$(readlink -f "$1" 2>/dev/null)")"
    [ -n "$part" ] && [ -e "$SYS_CLASS_BLOCK/$part" ] || return 1
    basename "$(dirname "$(readlink -f "$SYS_CLASS_BLOCK/$part")")"
}

# Per-vdev latency, as a real Prometheus histogram.
#
# WHY THIS EXISTS. On 2026-10-06 tank's mirror-2 was running roughly 50x the
# I/O latency of its three sibling vdevs (58ms read against 1ms) and every
# existing signal was green: the pool ONLINE, smart_device_health 1 on both
# members, zero reallocated, pending, uncorrectable and CRC, zero ata link
# resets. A vdev can be fifty times slower than its peers with nothing to
# show for it, so the slow one was found by hand with `zpool iostat` and
# could not be alerted on, trended, or even confirmed later. See #218.
#
# WHY A HISTOGRAM AND NOT A GAUGE. `zpool iostat -l` reports an AVERAGE over
# an interval, which means either blocking the collector for a sample window
# every run, or reporting a since-boot average that can never recover from a
# past event and hides every transient. `zpool iostat -w` instead gives
# cumulative bucket COUNTS since boot, which is exactly a Prometheus
# histogram: rate() over the buckets gives the latency distribution during
# any window, and histogram_quantile() turns that into a p99 that can be
# compared between sibling vdevs. It also costs nothing to read.
#
# The native buckets are powers of two from 1ns to ~137s, 37 of them, and
# almost all are empty. They are folded into the coarse boundaries below so
# the series count stays bounded: 14 vdevs x 4 metrics x 11 buckets.
emit_vdev_latency() {
    local pool="$1"
    zpool iostat -Hp -w -v "$pool" 2>/dev/null | awk -v pool="$pool" '
    BEGIN {
        n = split("100000 500000 1000000 5000000 10000000 50000000 100000000 500000000 1000000000 5000000000", edge, " ")
        split("0.0001 0.0005 0.001 0.005 0.01 0.05 0.1 0.5 1 5", le, " ")
        mname[1] = "total_wait_read";  mcol[1] = 2
        mname[2] = "total_wait_write"; mcol[2] = 3
        mname[3] = "disk_wait_read";   mcol[3] = 4
        mname[4] = "disk_wait_write";  mcol[4] = 5
        section = ""
    }
    function flush(   m, i, cum) {
        if (section == "") return
        for (m = 1; m <= 4; m++) {
            cum = 0
            for (i = 1; i <= n; i++) {
                cum += bin[m, i]
                printf "zfs_vdev_%s_seconds_bucket{pool=\"%s\",vdev=\"%s\",le=\"%s\"} %d\n", mname[m], pool, section, le[i], cum
            }
            cum += bin[m, n + 1]
            printf "zfs_vdev_%s_seconds_bucket{pool=\"%s\",vdev=\"%s\",le=\"+Inf\"} %d\n", mname[m], pool, section, cum
            printf "zfs_vdev_%s_seconds_count{pool=\"%s\",vdev=\"%s\"} %d\n", mname[m], pool, section, cum
        }
    }
    # A section header is a lone name: the pool, a top-level vdev, or a leaf.
    NF == 1 && $1 !~ /^[0-9]+$/ {
        flush()
        section = $1
        for (m = 1; m <= 4; m++) for (i = 1; i <= n + 1; i++) bin[m, i] = 0
        next
    }
    # Data row: bucket upper bound in ns, then 11 columns.
    NF >= 12 && $1 ~ /^[0-9]+$/ {
        b = $1 + 0
        slot = n + 1
        for (i = 1; i <= n; i++) if (b <= edge[i]) { slot = i; break }
        for (m = 1; m <= 4; m++) {
            v = $(mcol[m])
            if (v ~ /^[0-9]+$/) bin[m, slot] += v
        }
    }
    END { flush() }
    '
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

    echo "# HELP zfs_vdev_total_wait_read_seconds Read latency including queueing, per vdev."
    echo "# TYPE zfs_vdev_total_wait_read_seconds histogram"
    echo "# HELP zfs_vdev_total_wait_write_seconds Write latency including queueing, per vdev."
    echo "# TYPE zfs_vdev_total_wait_write_seconds histogram"
    echo "# HELP zfs_vdev_disk_wait_read_seconds Read latency at the disk, excluding ZFS queueing."
    echo "# TYPE zfs_vdev_disk_wait_read_seconds histogram"
    echo "# HELP zfs_vdev_disk_wait_write_seconds Write latency at the disk, excluding ZFS queueing."
    echo "# TYPE zfs_vdev_disk_wait_write_seconds histogram"
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        emit_vdev_latency "$p"
    done < <(zpool list -Hp -o name 2>/dev/null)

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
    echo "# HELP smart_scrape_ok Whether smartctl returned usable data for the device."
    echo "# TYPE smart_scrape_ok gauge"
    echo "# HELP smart_devices_total Devices smartctl was able to read this run."
    echo "# TYPE smart_devices_total gauge"

    devices_ok=0
    # Overridable so the failing-disk and unreadable-disk paths can be driven
    # against a stub smartctl in tests. Production always uses the default;
    # without it a test silently iterates the real disks of whatever host it
    # runs on, which passes or fails depending on the hardware present.
    for dev in ${SMART_DEV_GLOB:-/dev/sd?}; do
        [ -e "$dev" ] || continue
        d="${dev##*/}"
        bay="$(disk_bay "$d")"
        sys_serial="$(disk_serial "$d")"
        # scrape_ok carries bay and serial too: it is the series that fires
        # when a drive drops off the bus, and smartctl cannot read a serial
        # from a drive that has stopped answering.
        ok_lbl="device=\"$d\",bay=\"$bay\",serial=\"$sys_serial\""

        # smartctl packs FINDINGS into its exit status as a bitmask, and it
        # still prints full output alongside them:
        #   bits 0-2  invocation failed (bad args / open failed / command failed)
        #             -> no usable data
        #   bit  3    SMART status says DISK FAILING
        #   bit  4    a prefail attribute is at or below threshold
        #   bit  5    an attribute was below threshold in the past
        #   bit  6    the error log has errors
        #   bit  7    the self-test log has errors
        #
        # The previous `|| continue` treated every one of those as fatal, so a
        # drive reporting DISK FAILING was SKIPPED and emitted no metric at
        # all. That made the smart_device_health "0" branch unreachable: the
        # series could only ever be 1 or absent, and it went absent at exactly
        # the moment it mattered. Only bits 0-2 mean we have nothing to report.
        info="$(smartctl -i -A -H "$dev" 2>/dev/null)"
        rc=$?

        # Bits 0-1 mean we never reached the device, so there is nothing to
        # parse. Bit 2 is different: "some SMART or ATA command failed" can mean
        # a single attribute command failed while the health verdict arrived
        # fine, and smartctl still prints its banner and identity block either
        # way. Skipping on bit 2 therefore drops a disk we do have a verdict
        # for, which is a smaller version of the drop this script already had.
        # Whether the scrape was usable is decided below, on whether a verdict
        # actually arrived, rather than on the exit status alone.
        if [ "$((rc & 3))" -ne 0 ] || [ -z "$info" ]; then
            echo "smart_scrape_ok{$ok_lbl} 0"
            continue
        fi
        model="$(printf '%s' "$info" | awk -F: '/Device Model/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}')"
        serial="$(printf '%s' "$info" | awk -F: '/Serial Number/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}')"
        sup="$(printf '%s' "$info" | awk -F: '/SMART support is/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2}' | tail -1)"
        [ -z "$serial" ] && [ "$sys_serial" != unknown ] && serial="$sys_serial"
        lbl="device=\"$d\",bay=\"$bay\",model=\"${model:-unknown}\",serial=\"${serial:-unknown}\""

        # An absent "SMART support is" line means we could not read it, which is
        # not the same as SMART being switched off, so do not assert 0 there.
        case "$sup" in
            Enabled) echo "smart_enabled{$lbl} 1" ;;
            "")      : ;;
            *)       echo "smart_enabled{$lbl} 0" ;;
        esac

        # ATA prints "SMART overall-health self-assessment test result: PASSED".
        # SCSI and SAS print "SMART Health Status: OK" and also appear as
        # /dev/sdX, so matching only the ATA wording left such a disk with no
        # verdict at all: no health series, and smart_scrape_ok 0 forever.
        health="$(printf '%s' "$info" \
            | awk -F: '/overall-health|SMART Health Status/{gsub(/^[ \t]+|[ \t]+$/,"",$2); print $2; exit}')"

        # Exit bit 3 is authoritative for DISK FAILING. Trust it over the
        # printed line, whose wording varies between ATA, SCSI and NVMe.
        if [ "$((rc & 8))" -ne 0 ]; then
            health_code=0
        else
            case "$health" in
                PASSED|OK) health_code=1 ;;
                "")        health_code="" ;;
                *)         health_code=0 ;;
            esac
        fi

        # The scrape counts as usable only if a verdict actually arrived.
        # Otherwise a disk whose SMART commands are failing would report
        # smart_scrape_ok 1 with no health series at all, which is the same
        # silent hole as before, one layer further in.
        if [ -n "$health_code" ]; then
            echo "smart_scrape_ok{$ok_lbl} 1"
            devices_ok=$((devices_ok + 1))
            echo "smart_device_health{$lbl} $health_code"
        else
            echo "smart_scrape_ok{$ok_lbl} 0"
        fi

        # Findings that do not fail the overall health check but predict it.
        echo "smart_prefail_below_threshold{$lbl} $([ "$((rc & 16))" -ne 0 ] && echo 1 || echo 0)"
        echo "smart_error_log_has_errors{$lbl} $([ "$((rc & 64))" -ne 0 ] && echo 1 || echo 0)"

        poh="$(printf '%s' "$info" | awk '/Power_On_Hours/{print $10; exit}')"
        crc="$(printf '%s' "$info" | awk '/CRC_Error_Count/{print $10; exit}')"
        ral="$(printf '%s' "$info" | awk '/Reallocated_Sector/{print $10; exit}')"
        tmp="$(printf '%s' "$info" | awk '/Temperature_Celsius|Airflow_Temperature/{print $10; exit}')"
        [ -n "${poh:-}" ] && echo "smart_power_on_hours{$lbl} $poh"
        [ -n "${crc:-}" ] && echo "smart_crc_error_count{$lbl} $crc"
        [ -n "${ral:-}" ] && echo "smart_reallocated_sector_count{$lbl} $ral"
        [ -n "${tmp:-}" ] && echo "smart_temperature_celsius{$lbl} $tmp"
    done
    echo "smart_devices_total $devices_ok"

    # Per-disk ZFS state. Pool health alone is not enough: on 2026-10-08 a
    # mirror member was FAULTED for half an hour while `zpool list` still
    # said the pool was ONLINE, so the pool-level alert never fired.
    echo "# HELP zfs_vdev_state Leaf vdev state: 0=ONLINE 1=DEGRADED 2=FAULTED 3=OFFLINE 4=UNAVAIL 5=REMOVED 6=UNKNOWN."
    echo "# TYPE zfs_vdev_state gauge"
    echo "# HELP zfs_vdev_read_errors Read errors ZFS has counted on the vdev since the last clear."
    echo "# TYPE zfs_vdev_read_errors gauge"
    echo "# HELP zfs_vdev_write_errors Write errors ZFS has counted on the vdev since the last clear."
    echo "# TYPE zfs_vdev_write_errors gauge"
    echo "# HELP zfs_vdev_checksum_errors Checksum errors ZFS has counted on the vdev since the last clear."
    echo "# TYPE zfs_vdev_checksum_errors gauge"
    echo "# HELP zfs_vdev_scrape_ok Whether zpool status -j could be read and parsed."
    echo "# TYPE zfs_vdev_scrape_ok gauge"

    # Last known bay and serial per vdev guid. When a disk is pulled or drops
    # off the bus its by-partuuid link disappears, and that is exactly the
    # moment the alert has to say which tray it was. Hidden and not *.prom, so
    # node_exporter ignores it.
    CACHE="${OUT_DIR}/.zfs_vdev_labels"
    NEW_CACHE="$(mktemp "${OUT_DIR}/.zfs_vdev_labels.XXXXXX")"

    # One line per leaf vdev: pool, top-level vdev, guid, name, path, state,
    # read, write, checksum. Spares report AVAIL or INUSE rather than ONLINE.
    # shellcheck disable=SC2016
    jq_leaves='
      .pools[] as $p
      | ( [ ($p.vdevs // {})[]?.vdevs // {} | to_entries[] | {top: .key, v: .value} ]
          + [ ($p.logs // {}), ($p.l2cache // {}), ($p.spares // {})
              | to_entries[] | {top: .value.class, v: .value} ] )[]
      | .top as $top
      | .v | recurse(.vdevs // {} | .[])
      | select(.vdev_type == "disk" or .vdev_type == "file")
      | [$p.name, $top, .guid, .name, (.path // ""), .state,
         (.read_errors // "0"), (.write_errors // "0"), (.checksum_errors // "0")]
      | @tsv'
    if status_json="$(zpool status -j -p 2>/dev/null)" && [ -n "$status_json" ] \
        && leaves="$(printf '%s' "$status_json" | jq -r "$jq_leaves" 2>/dev/null)"; then
        echo "zfs_vdev_scrape_ok 1"
        while IFS=$'\t' read -r pool top guid name path state rd wr ck; do
            [ -z "${guid:-}" ] && continue
            if d="$(disk_of_path "$path")"; then
                bay="$(disk_bay "$d")"; serial="$(disk_serial "$d")"
            else
                d=unknown; bay=unknown; serial=unknown
            fi
            if [ "$bay" = unknown ] || [ "$serial" = unknown ]; then
                cached="$(awk -F'\t' -v g="$guid" '$1 == g {print $2 "\t" $3; exit}' "$CACHE" 2>/dev/null)"
                if [ -n "$cached" ]; then
                    [ "$bay" = unknown ] && bay="${cached%%$'\t'*}"
                    [ "$serial" = unknown ] && serial="${cached#*$'\t'}"
                fi
            fi
            printf '%s\t%s\t%s\n' "$guid" "$bay" "$serial" >> "$NEW_CACHE"
            case "$state" in AVAIL|INUSE) code=0 ;; *) code="$(health_code "$state")" ;; esac
            l="pool=\"$pool\",vdev=\"$top\",guid=\"$guid\",name=\"$name\",device=\"$d\",bay=\"$bay\",serial=\"$serial\""
            echo "zfs_vdev_state{$l} $code"
            echo "zfs_vdev_read_errors{$l} $rd"
            echo "zfs_vdev_write_errors{$l} $wr"
            echo "zfs_vdev_checksum_errors{$l} $ck"
        done <<< "$leaves"
        chmod 0600 "$NEW_CACHE"
        mv "$NEW_CACHE" "$CACHE"
    else
        echo "zfs_vdev_scrape_ok 0"
        rm -f "$NEW_CACHE"
    fi

    echo "# HELP zfs_smart_textfile_last_run_timestamp_seconds Unix time of the last successful run."
    echo "# TYPE zfs_smart_textfile_last_run_timestamp_seconds gauge"
    echo "zfs_smart_textfile_last_run_timestamp_seconds $(date +%s)"
} > "$TMP"

chmod 0644 "$TMP"
mv "$TMP" "$OUT"
trap - EXIT
