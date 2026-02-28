#!/usr/bin/env bash
set -euo pipefail

# Fujifilm X-T30 III → Immich auto-import pipeline
# Downloads new photos from camera via gphoto2 and uploads to Immich via CLI.
# Immich uploader runs in parallel (watch mode) so photos appear while downloading.
# Writes progress to status.json for the Homepage dashboard widget.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOMELAB_DIR="$(dirname "$SCRIPT_DIR")"
IMPORT_DIR="$SCRIPT_DIR/import"
LOG_FILE="$SCRIPT_DIR/import.log"
STATUS_FILE="$SCRIPT_DIR/status.json"

if [[ -f "$HOMELAB_DIR/.env" ]]; then
    set -a
    source "$HOMELAB_DIR/.env"
    set +a
fi

IMMICH_URL="${IMMICH_URL:-http://localhost:2283}"
IMMICH_API_KEY="${IMMICH_API_KEY:-}"
IMMICH_ALBUM="${IMMICH_ALBUM:-Fuji-XT30}"

IMMICH_WATCHER_PID=""

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

die() {
    log "ERROR: $*"
    write_status "error" "$*" 0 0
    stop_immich_watcher
    exit 1
}

write_status() {
    local status="$1" message="$2" downloaded="${3:-0}" total="${4:-0}"
    local percent=0
    if [[ "$total" -gt 0 ]]; then
        percent=$(( (downloaded * 100) / total ))
    fi
    cat > "$STATUS_FILE" << EOF
{
  "status": "${status}",
  "message": "${message}",
  "downloaded": ${downloaded},
  "total": ${total},
  "percent": ${percent},
  "timestamp": "$(date '+%Y-%m-%d %H:%M:%S')"
}
EOF
}

cleanup_grabbers() {
    for proc in gvfs-gphoto2-volume-monitor gvfsd-gphoto2; do
        if pgrep -x "$proc" > /dev/null 2>&1; then
            log "Killing $proc to release camera..."
            pkill -x "$proc" 2>/dev/null || true
            sleep 1
        fi
    done
}

start_immich_watcher() {
    if [[ -z "$IMMICH_API_KEY" ]]; then
        log "WARN: IMMICH_API_KEY not set in .env -- Immich watcher skipped."
        return 0
    fi
    if ! command -v immich &>/dev/null; then
        log "WARN: immich CLI not installed -- Immich watcher skipped."
        return 0
    fi

    log "Starting Immich watcher (album: $IMMICH_ALBUM) ..."

    IMMICH_INSTANCE_URL="$IMMICH_URL" \
    IMMICH_API_KEY="$IMMICH_API_KEY" \
    immich upload \
        --recursive \
        --watch \
        --album-name "$IMMICH_ALBUM" \
        --no-progress \
        "$IMPORT_DIR" >> "$LOG_FILE" 2>&1 &

    IMMICH_WATCHER_PID=$!
    log "Immich watcher started (PID: $IMMICH_WATCHER_PID)"
}

stop_immich_watcher() {
    if [[ -n "$IMMICH_WATCHER_PID" ]] && kill -0 "$IMMICH_WATCHER_PID" 2>/dev/null; then
        log "Waiting for Immich watcher to finish uploading..."
        sleep 10
        kill "$IMMICH_WATCHER_PID" 2>/dev/null || true
        wait "$IMMICH_WATCHER_PID" 2>/dev/null || true
        log "Immich watcher stopped."
        IMMICH_WATCHER_PID=""
    fi
}

reset_usb() {
    local usb_dev
    usb_dev="$(lsusb | grep '04cb:' | head -1 | sed 's/Bus \([0-9]*\) Device \([0-9]*\).*/\/dev\/bus\/usb\/\1\/\2/')"
    if [[ -n "$usb_dev" && -e "$usb_dev" ]]; then
        log "Resetting USB device $usb_dev ..."
        python3 -c "
import fcntl, os
fd = os.open('$usb_dev', os.O_WRONLY)
fcntl.ioctl(fd, 0x5514, 0)
os.close(fd)
" 2>/dev/null || true
        sleep 3
    else
        sleep 2
    fi
}

count_camera_files() {
    # Retry loop for PTP initialization after USB reset
    local file_count=0 attempt
    for attempt in 1 2 3 4 5; do
        file_count="$(gphoto2 --list-files 2>/dev/null | grep -c "^#" || true)"
        file_count="${file_count:-0}"
        if [[ "$file_count" -gt 0 ]]; then
            break
        fi
        log "Attempt $attempt: no files listed, retrying in 5s..."
        sleep 5
    done
    echo "$file_count"
}

progress_monitor() {
    # Background process that polls file count in dest dir and updates status.json
    local dest="$1" total="$2" pre_existing="$3"
    while true; do
        local current
        current="$(find "$dest" -maxdepth 1 -type f 2>/dev/null | wc -l)"
        current="${current// /}"
        local new_files=$(( current - pre_existing ))
        [[ "$new_files" -lt 0 ]] && new_files=0
        local last_file
        last_file="$(ls -t "$dest" 2>/dev/null | grep -v "^tmpfile" | head -1)"
        write_status "running" "Downloading ${current}/${total}: ${last_file:-...}" "$current" "$total"
        sleep 2
    done
}

download_photos() {
    local today dest
    today="$(date '+%Y-%m-%d')"
    dest="$IMPORT_DIR/$today"
    mkdir -p "$dest"

    write_status "running" "Counting files on camera..." 0 0

    local total
    total="$(count_camera_files)"

    if [[ "$total" -eq 0 ]]; then
        log "No files found on camera."
        write_status "idle" "No files on camera" 0 0
        rmdir "$dest" 2>/dev/null || true
        return 1
    fi

    local pre_existing
    pre_existing="$(find "$dest" -maxdepth 1 -type f 2>/dev/null | wc -l)"
    pre_existing="${pre_existing// /}"

    log "Found $total file(s) on camera ($pre_existing already downloaded). Batch downloading to $dest ..."
    write_status "running" "Downloading to $dest ..." "$pre_existing" "$total"

    # Start background progress monitor
    progress_monitor "$dest" "$total" "$pre_existing" &
    local monitor_pid=$!

    cd "$dest"

    local max_retries=3 attempt=1
    while [[ "$attempt" -le "$max_retries" ]]; do
        log "Download attempt $attempt/$max_retries ..."
        gphoto2 --get-all-files --skip-existing 2>&1 | tee -a "$LOG_FILE"
        local gphoto_exit=${PIPESTATUS[0]}

        if [[ "$gphoto_exit" -eq 0 ]]; then
            break
        fi

        log "gphoto2 exited with code $gphoto_exit (PTP error). Resetting USB and retrying..."
        write_status "running" "PTP error -- resetting USB (retry $attempt/$max_retries)..." 0 "$total"
        cd "$SCRIPT_DIR"
        cleanup_grabbers
        reset_usb
        cd "$dest"
        attempt=$(( attempt + 1 ))
    done

    cd "$SCRIPT_DIR"

    # Stop progress monitor
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true

    local final_count
    final_count="$(find "$dest" -maxdepth 1 -type f 2>/dev/null | wc -l)"
    final_count="${final_count// /}"
    NEW_COUNT=$(( final_count - pre_existing ))
    [[ "$NEW_COUNT" -lt 0 ]] && NEW_COUNT=0
    DOWNLOADED_COUNT="$final_count"

    if [[ "$final_count" -eq 0 ]]; then
        rmdir "$dest" 2>/dev/null || true
        log "No new photos to download."
        write_status "idle" "No new photos found" 0 0
        return 1
    fi

    log "Download complete: $final_count total files ($NEW_COUNT new)."
    return 0
}

main() {
    log "========================================="
    log "Photo import started (album: $IMMICH_ALBUM)"
    log "========================================="

    DOWNLOADED_COUNT=0
    NEW_COUNT=0

    trap 'stop_immich_watcher' EXIT

    if ! command -v gphoto2 &>/dev/null; then
        die "gphoto2 is not installed. Run: sudo apt install gphoto2"
    fi

    write_status "running" "Detecting camera..." 0 0
    cleanup_grabbers
    reset_usb

    local detected
    detected="$(gphoto2 --auto-detect 2>/dev/null)"
    if ! echo "$detected" | grep -q "usb:"; then
        die "No camera detected. Is it connected via USB and powered on?"
    fi

    log "Camera detected:"
    echo "$detected" | tee -a "$LOG_FILE"

    mkdir -p "$IMPORT_DIR"

    # Start Immich watcher in parallel so photos upload while downloading
    start_immich_watcher

    if download_photos; then
        write_status "running" "Uploading final batch to Immich..." "$DOWNLOADED_COUNT" "$DOWNLOADED_COUNT"
        stop_immich_watcher

        # Final sweep to catch anything the watcher missed
        if [[ -n "$IMMICH_API_KEY" ]] && command -v immich &>/dev/null; then
            log "Running final Immich upload sweep..."
            IMMICH_INSTANCE_URL="$IMMICH_URL" \
            IMMICH_API_KEY="$IMMICH_API_KEY" \
            immich upload \
                --recursive \
                --album-name "$IMMICH_ALBUM" \
                --no-progress \
                "$IMPORT_DIR" >> "$LOG_FILE" 2>&1 || true
        fi

        write_status "idle" "Imported ${NEW_COUNT} new photos to ${IMMICH_ALBUM}" "$DOWNLOADED_COUNT" "$DOWNLOADED_COUNT"
    fi

    log "========================================="
    log "Photo import finished"
    log "========================================="
}

main "$@"
