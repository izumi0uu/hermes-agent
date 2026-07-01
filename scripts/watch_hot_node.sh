#!/usr/bin/env bash
# Watch for hot node/codex processes and capture enough context to identify them
# after they exit.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
WATCH_LOG_ROOT="${WATCH_LOG_ROOT:-$HERMES_HOME/logs/node-hot-watch}"
WATCH_CPU_THRESHOLD="${WATCH_CPU_THRESHOLD:-80}"
WATCH_INTERVAL_SEC="${WATCH_INTERVAL_SEC:-2}"
WATCH_SAMPLE_DURATION_SEC="${WATCH_SAMPLE_DURATION_SEC:-5}"
WATCH_SAMPLE_INTERVAL_MS="${WATCH_SAMPLE_INTERVAL_MS:-10}"
WATCH_COOLDOWN_SEC="${WATCH_COOLDOWN_SEC:-120}"
WATCH_VERBOSE="${WATCH_VERBOSE:-0}"
SCRIPT_PATH="${SCRIPT_PATH:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")}"
PID_FILE="$WATCH_LOG_ROOT/watcher.pid"
COOLDOWN_DIR="$WATCH_LOG_ROOT/.cooldowns"
LAUNCHD_LABEL="${LAUNCHD_LABEL:-dev.hermes.node-hot-watch}"
LAUNCHD_PLIST="${LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/$LAUNCHD_LABEL.plist}"
RUN_ONCE=false

usage() {
    cat <<'EOF'
Usage: scripts/watch_hot_node.sh [--once] [--install-launch-agent] [--stop] [--status]

Environment overrides:
  WATCH_CPU_THRESHOLD       CPU percent threshold to trigger capture (default: 80)
  WATCH_INTERVAL_SEC        Poll interval in seconds (default: 2)
  WATCH_SAMPLE_DURATION_SEC sample duration in seconds (default: 5)
  WATCH_SAMPLE_INTERVAL_MS  sample interval in milliseconds (default: 10)
  WATCH_COOLDOWN_SEC        Per-PID cooldown before re-capturing (default: 120)
  WATCH_VERBOSE             1 logs idle scan results; 0 stays quiet unless triggered
  WATCH_LOG_ROOT            Output directory (default: ~/.hermes/logs/node-hot-watch)
  LAUNCHD_LABEL             LaunchAgent label when installed (default: dev.hermes.node-hot-watch)

Each capture creates an event directory with:
  - ps snapshots
  - parent process chain
  - cwd and open files
  - network sockets
  - a short macOS sample profile
EOF
}

log_line() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

verbose_log() {
    if [ "$WATCH_VERBOSE" = "1" ]; then
        log_line "$*"
    fi
}

is_running_pid() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

cleanup_pidfile() {
    if [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE" 2>/dev/null || true)" = "$$" ]; then
        rm -f "$PID_FILE"
    fi
}

start_guard() {
    mkdir -p "$WATCH_LOG_ROOT" "$COOLDOWN_DIR"

    if [ -f "$PID_FILE" ]; then
        local existing_pid
        existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        if is_running_pid "$existing_pid"; then
            log_line "Watcher already running with PID $existing_pid"
            exit 1
        fi
        rm -f "$PID_FILE"
    fi

    echo "$$" >"$PID_FILE"
    trap cleanup_pidfile EXIT INT TERM HUP
}

stop_watcher() {
    launchctl bootout "gui/$(id -u)" "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
    launchctl remove "$LAUNCHD_LABEL" >/dev/null 2>&1 || true

    if [ ! -f "$PID_FILE" ]; then
        log_line "Watcher stopped"
        return 0
    fi

    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if is_running_pid "$pid"; then
        kill "$pid"
        log_line "Sent TERM to watcher PID $pid"
    else
        log_line "Removing stale pidfile for PID $pid"
        rm -f "$PID_FILE"
    fi
}

status_watcher() {
    if launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; then
        log_line "Watcher is loaded in launchd as $LAUNCHD_LABEL"
        return 0
    fi

    if [ ! -f "$PID_FILE" ]; then
        log_line "Watcher is not running"
        return 1
    fi

    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if is_running_pid "$pid"; then
        log_line "Watcher is running with PID $pid"
        return 0
    fi

    log_line "Watcher pidfile exists but PID $pid is not running"
    return 1
}

write_launch_agent_plist() {
    mkdir -p "$(dirname "$LAUNCHD_PLIST")" "$WATCH_LOG_ROOT"
    cat >"$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LAUNCHD_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_PATH</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>WATCH_VERBOSE</key>
        <string>$WATCH_VERBOSE</string>
        <key>WATCH_LOG_ROOT</key>
        <string>$WATCH_LOG_ROOT</string>
        <key>WATCH_CPU_THRESHOLD</key>
        <string>$WATCH_CPU_THRESHOLD</string>
        <key>WATCH_INTERVAL_SEC</key>
        <string>$WATCH_INTERVAL_SEC</string>
        <key>WATCH_SAMPLE_DURATION_SEC</key>
        <string>$WATCH_SAMPLE_DURATION_SEC</string>
        <key>WATCH_SAMPLE_INTERVAL_MS</key>
        <string>$WATCH_SAMPLE_INTERVAL_MS</string>
        <key>WATCH_COOLDOWN_SEC</key>
        <string>$WATCH_COOLDOWN_SEC</string>
        <key>LAUNCHD_LABEL</key>
        <string>$LAUNCHD_LABEL</string>
        <key>LAUNCHD_PLIST</key>
        <string>$LAUNCHD_PLIST</string>
        <key>SCRIPT_PATH</key>
        <string>$SCRIPT_PATH</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$WATCH_LOG_ROOT/watcher.log</string>

    <key>StandardErrorPath</key>
    <string>$WATCH_LOG_ROOT/watcher.log</string>

    <key>WorkingDirectory</key>
    <string>$(dirname "$SCRIPT_PATH")</string>
</dict>
</plist>
EOF
}

install_launch_agent() {
    write_launch_agent_plist
    launchctl bootout "gui/$(id -u)" "$LAUNCHD_PLIST" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$LAUNCHD_PLIST"
    log_line "Installed launchd watcher as $LAUNCHD_LABEL"
}

candidate_pids() {
    {
        pgrep -x node 2>/dev/null || true
        pgrep -x codex 2>/dev/null || true
    } | awk 'NF {print $1}' | sort -u
}

pid_cpu() {
    local pid="$1"
    ps -p "$pid" -o %cpu= 2>/dev/null | awk '{print $1}' || true
}

pid_ppid() {
    local pid="$1"
    ps -p "$pid" -o ppid= 2>/dev/null | awk '{print $1}' || true
}

cpu_ge_threshold() {
    local cpu="${1:-0}"
    awk -v cpu="$cpu" -v threshold="$WATCH_CPU_THRESHOLD" 'BEGIN { exit !(cpu >= threshold) }'
}

cooldown_ready() {
    local pid="$1"
    local file="$COOLDOWN_DIR/$pid.last"
    local now last elapsed

    now="$(date +%s)"
    if [ -f "$file" ]; then
        last="$(cat "$file" 2>/dev/null || echo 0)"
        elapsed=$((now - last))
        if [ "$elapsed" -lt "$WATCH_COOLDOWN_SEC" ]; then
            return 1
        fi
    fi

    echo "$now" >"$file"
    return 0
}

write_parent_chain() {
    local pid="$1"
    local outfile="$2"
    local current="$pid"

    while [ -n "$current" ] && [ "$current" -gt 0 ] 2>/dev/null; do
        ps -ww -p "$current" -o pid=,ppid=,pgid=,etime=,state=,%cpu=,%mem=,comm=,args= \
            >>"$outfile" 2>/dev/null || break
        current="$(pid_ppid "$current")"
    done
}

capture_pid() {
    local pid="$1"
    local stamp event_dir summary_file cwd_path parent_pid

    stamp="$(date '+%Y%m%d-%H%M%S')"
    event_dir="$WATCH_LOG_ROOT/event-$stamp-pid-$pid"
    mkdir -p "$event_dir"
    summary_file="$event_dir/summary.txt"

    {
        echo "timestamp: $(date '+%Y-%m-%d %H:%M:%S %z')"
        echo "pid: $pid"
        echo "threshold: $WATCH_CPU_THRESHOLD"
        echo "sample_duration_sec: $WATCH_SAMPLE_DURATION_SEC"
        echo "sample_interval_ms: $WATCH_SAMPLE_INTERVAL_MS"
        echo
    } >"$summary_file"

    ps -ww -p "$pid" -o pid,ppid,pgid,etime,state,%cpu,%mem,user,comm,args \
        >"$event_dir/ps.txt" 2>&1 || true
    ps -ww -p "$pid" -o lstart= >"$event_dir/start-time.txt" 2>&1 || true

    parent_pid="$(pid_ppid "$pid")"
    {
        echo "target"
        ps -ww -p "$pid" -o pid,ppid,pgid,etime,state,%cpu,%mem,user,comm,args
        if [ -n "$parent_pid" ]; then
            echo
            echo "parent"
            ps -ww -p "$parent_pid" -o pid,ppid,pgid,etime,state,%cpu,%mem,user,comm,args
        fi
    } >"$event_dir/target-and-parent.txt" 2>&1 || true

    write_parent_chain "$pid" "$event_dir/parent-chain.txt"

    lsof -a -p "$pid" -d cwd -Fn >"$event_dir/cwd.txt" 2>&1 || true
    cwd_path="$(sed -n 's/^n//p' "$event_dir/cwd.txt" | head -1)"
    if [ -n "$cwd_path" ]; then
        echo "cwd: $cwd_path" >>"$summary_file"
    fi

    lsof -a -p "$pid" -n -P >"$event_dir/open-files.txt" 2>&1 || true
    lsof -a -p "$pid" -i -n -P >"$event_dir/network.txt" 2>&1 || true

    if sample "$pid" "$WATCH_SAMPLE_DURATION_SEC" "$WATCH_SAMPLE_INTERVAL_MS" \
        -mayDie -file "$event_dir/sample.txt" \
        >"$event_dir/sample.stdout.txt" 2>"$event_dir/sample.stderr.txt"; then
        echo "sample: ok" >>"$summary_file"
    else
        echo "sample: failed" >>"$summary_file"
    fi

    log_line "Captured hot process PID $pid into $event_dir"
}

scan_once() {
    local tmp_file pid cpu candidate_count

    tmp_file="$(mktemp)"
    trap 'rm -f "$tmp_file"' RETURN
    candidate_count=0

    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        cpu="$(pid_cpu "$pid")"
        [ -n "$cpu" ] || continue
        candidate_count=$((candidate_count + 1))
        if cpu_ge_threshold "$cpu"; then
            printf '%012.3f %s\n' "$cpu" "$pid" >>"$tmp_file"
        fi
    done < <(candidate_pids)

    if [ "$candidate_count" -eq 0 ]; then
        verbose_log "No node/codex processes found"
        return 0
    fi

    if [ ! -s "$tmp_file" ]; then
        verbose_log "No node/codex process is above ${WATCH_CPU_THRESHOLD}% CPU"
        return 0
    fi

    while read -r _cpu pid; do
        if cooldown_ready "$pid"; then
            capture_pid "$pid"
            return 0
        fi
        verbose_log "Skipping PID $pid because it is still in cooldown"
    done < <(sort -nr "$tmp_file")

    return 0
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --once)
            RUN_ONCE=true
            ;;
        --install-launch-agent)
            install_launch_agent
            exit 0
            ;;
        --stop)
            stop_watcher
            exit 0
            ;;
        --status)
            status_watcher
            exit $?
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

start_guard
verbose_log "Watching node/codex processes in $WATCH_LOG_ROOT"
verbose_log "Trigger: CPU >= ${WATCH_CPU_THRESHOLD}%, interval ${WATCH_INTERVAL_SEC}s, sample ${WATCH_SAMPLE_DURATION_SEC}s"

if [ "$RUN_ONCE" = true ]; then
    scan_once
    exit 0
fi

while true; do
    scan_once
    sleep "$WATCH_INTERVAL_SEC"
done
