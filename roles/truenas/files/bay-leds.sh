#!/usr/bin/env bash
# Drives the DXP8800 Plus front-panel bay LEDs from disk activity and ZFS state.
#
#   dim white, steady   disk present, idle
#   white blink         disk is reading or writing
#   amber blink         ZFS counted errors on it, or SMART cannot be read
#   red, steady         its vdev is not ONLINE: this is the tray to pull
#   off                 empty bay
#   purple, slow pulse  this service is not running; do not trust the panel
#
# Bay N is kernel port ataN, the Nth tray from the left (verified by LED and a
# live reseat on 2026-10-08). Fault state is read from the textfile the
# zfs-smart-textfile exporter already writes every minute, so this never runs
# zpool or touches a drive. Activity comes from /sys/class/block/<dev>/stat,
# which is a kernel counter read, also with no I/O to the drive.
#
# The LED controller shares the I2C bus with board sensors, so an LED is only
# written when that bay's state changes, never on every poll.
#
# Usage: bay-leds.sh            run the loop
#        bay-leds.sh --stopped  paint every bay "service not running" and exit
set -uo pipefail

LED_CLI="${LED_CLI:-/var/lib/node_exporter/ugreen_leds_cli}"
SYS_CLASS_BLOCK="${SYS_CLASS_BLOCK:-/sys/class/block}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfiles}"
PROM="${PROM_FILE:-${TEXTFILE_DIR}/zfs_smart.prom}"
HEARTBEAT="${TEXTFILE_DIR}/bay_leds.prom"
# While this file exists the service writes nothing, so a person can light a
# tray by hand (ugreen_leds_cli diskN ...) during a drive swap without the
# loop painting over it. It lives in /run, so a reboot cannot leave it stuck.
HOLD_FILE="${HOLD_FILE:-/run/bay-leds.hold}"
POLL="${POLL_SECONDS:-0.5}"
# Fault data older than this means the exporter has stopped, and a panel
# showing white would then be claiming health nobody measured.
STALE_AFTER="${STALE_AFTER_SECONDS:-300}"
# Keep a bay blinking this many polls after its last I/O. ZFS flushes in
# bursts every few seconds, and without a hold every bay would flap between
# blink and idle on each flush, which reads badly and doubles the I2C writes.
ACTIVE_HOLD="${ACTIVE_HOLD_POLLS:-2}"
# For tests: stop after this many polls. 0 means run forever.
ITERATIONS="${ITERATIONS:-0}"
BAYS="${BAYS:-8}"

led() { "$LED_CLI" "$@" >/dev/null 2>&1; }

# Every LED look, as the CLI arguments that produce it.
look_args() {
    case "$1" in
        idle)    echo "-color 255 255 255 -on -brightness 40" ;;
        active)  echo "-color 255 255 255 -blink 80 80 -brightness 200" ;;
        warn)    echo "-color 255 120 0 -blink 500 500 -brightness 255" ;;
        fault)   echo "-color 255 0 0 -on -brightness 255" ;;
        empty)   echo "-off" ;;
        nodata)  echo "-color 160 0 255 -breath 2000 2000 -brightness 120" ;;
    esac
}

paint_stopped() {
    local b
    for b in $(seq 1 "$BAYS"); do
        # shellcheck disable=SC2046
        led "disk$b" $(look_args nodata)
    done
}

if [ "${1:-}" = "--stopped" ]; then
    paint_stopped
    exit 0
fi

# bay -> current device name, rebuilt every poll because sd letters move.
declare -A DEV_OF
map_devices() {
    DEV_OF=()
    local p d port
    for p in "$SYS_CLASS_BLOCK"/sd*; do
        [ -e "$p" ] || continue
        d="${p##*/}"
        # Partitions are listed here too; only whole disks have a device link.
        [ -e "$p/device" ] || continue
        port="$(readlink -f "$p" 2>/dev/null | grep -o 'ata[0-9]*' | head -1)"
        [ -n "$port" ] && DEV_OF["${port#ata}"]="$d"
    done
}

# bay -> fault level from the exporter's metrics: fault, warn, or unset.
declare -A FAULT_OF
PROM_FRESH=0
read_faults() {
    FAULT_OF=()
    PROM_FRESH=0
    [ -r "$PROM" ] || return
    local ts now
    ts="$(awk '/^zfs_smart_textfile_last_run_timestamp_seconds /{print int($2)}' "$PROM")"
    now="$(date +%s)"
    [ -n "$ts" ] && [ $((now - ts)) -le "$STALE_AFTER" ] || return
    PROM_FRESH=1
    # Each sample: metric{...,bay="N",...} value. Red beats amber.
    while read -r level bay; do
        [ -z "$bay" ] && continue
        [ "${FAULT_OF[$bay]:-}" = fault ] && continue
        FAULT_OF[$bay]="$level"
    done < <(awk '
        match($0, /bay="[^"]*"/) {
            bay = substr($0, RSTART + 5, RLENGTH - 6); v = $NF + 0
            if ($0 ~ /^zfs_vdev_state\{/ && v != 0)                 print "fault", bay
            else if ($0 ~ /^zfs_vdev_(read|write|checksum)_errors\{/ && v > 0) print "warn", bay
            else if ($0 ~ /^smart_scrape_ok\{/ && v == 0)          print "warn", bay
            else if ($0 ~ /^smart_device_health\{/ && v == 0)      print "fault", bay
        }' "$PROM")
}

io_count() {
    # Fields 1 and 5 of the block stat file: reads and writes completed.
    awk '{print $1 + $5}' "$SYS_CLASS_BLOCK/$1/stat" 2>/dev/null
}

heartbeat() {
    local tmp
    tmp="$(mktemp "${TEXTFILE_DIR}/.bay_leds.XXXXXX")" || return
    {
        echo "# HELP bay_leds_last_loop_timestamp_seconds Unix time the bay LED service last completed a poll."
        echo "# TYPE bay_leds_last_loop_timestamp_seconds gauge"
        echo "bay_leds_last_loop_timestamp_seconds $(date +%s)"
        echo "# HELP bay_leds_fault_data_fresh Whether the LEDs are showing current ZFS state (1) or the no-data pulse (0)."
        echo "# TYPE bay_leds_fault_data_fresh gauge"
        echo "bay_leds_fault_data_fresh $PROM_FRESH"
    } > "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$HEARTBEAT"
}

trap 'paint_stopped; exit 0' TERM INT

declare -A SHOWN LAST_IO LAST_ACTIVE
n=0
last_beat=0
while :; do
    map_devices
    read_faults
    if [ ! -e "$HOLD_FILE" ]; then
        for b in $(seq 1 "$BAYS"); do
            d="${DEV_OF[$b]:-}"
            if [ "$PROM_FRESH" -eq 0 ]; then
                want=nodata
            elif [ -n "${FAULT_OF[$b]:-}" ]; then
                want="${FAULT_OF[$b]}"
            elif [ -z "$d" ]; then
                want=empty
            else
                io="$(io_count "$d")"
                if [ -n "${LAST_IO[$b]:-}" ] && [ -n "$io" ] && [ "$io" != "${LAST_IO[$b]}" ]; then
                    LAST_ACTIVE[$b]="$n"
                fi
                LAST_IO[$b]="$io"
                if [ -n "${LAST_ACTIVE[$b]:-}" ] && [ $((n - LAST_ACTIVE[$b])) -lt "$ACTIVE_HOLD" ]; then
                    want=active
                else
                    want=idle
                fi
            fi
            if [ "${SHOWN[$b]:-}" != "$want" ]; then
                # shellcheck disable=SC2046
                led "disk$b" $(look_args "$want") && SHOWN[$b]="$want"
            fi
        done
    else
        # Forget what is shown, so leaving hold repaints every bay.
        SHOWN=()
    fi
    now="$(date +%s)"
    if [ $((now - last_beat)) -ge 15 ]; then
        heartbeat
        last_beat="$now"
    fi
    n=$((n + 1))
    [ "$ITERATIONS" -gt 0 ] && [ "$n" -ge "$ITERATIONS" ] && exit 0
    sleep "$POLL"
done
