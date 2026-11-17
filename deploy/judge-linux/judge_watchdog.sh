#!/usr/bin/env bash
set -Eeuo pipefail

WORK_ROOT=${JUDGE_WORK_ROOT:-/var/lib/xinbaoyan-judge/work}
META_ROOT=${JUDGE_META_ROOT:-/var/lib/xinbaoyan-judge/meta}
# isolate's default box root; /tmp/box-* was wrong (boxes never live there,
# and the worker's PrivateTmp makes its /tmp invisible to this host watchdog).
ISOLATE_BOX_ROOT=${JUDGE_ISOLATE_BOX_ROOT:-/var/local/lib/isolate}
ISOLATE_BIN=${JUDGE_ISOLATE_PATH:-/usr/bin/isolate}
# 2h default: long multi-case tasks can legitimately run past one hour; the
# execution lease is the authoritative liveness signal, this is only a sweep
# for crashed-run leftovers.
MAX_AGE_SECONDS=${JUDGE_WATCHDOG_MAX_AGE_SECONDS:-7200}
MAX_WORKER_MEM_MB=${JUDGE_WATCHDOG_MAX_WORKER_MEM_MB:-2048}
SERVICE=${XINBAOYAN_JUDGE_SERVICE:-xinbaoyan-judge-worker.service}

# Restart only on failure. An operator-stopped service (maintenance window)
# reports ActiveState=inactive with Result=success and must stay stopped.
active_state=$(systemctl show -p ActiveState --value "$SERVICE")
last_result=$(systemctl show -p Result --value "$SERVICE")
if [[ $active_state == failed || ( $active_state == inactive && $last_result != success ) ]]; then
    systemctl restart "$SERVICE"
fi

worker_main_pid=$(systemctl show -p MainPID --value "$SERVICE")
if [[ $worker_main_pid =~ ^[0-9]+$ && $worker_main_pid -gt 0 ]]; then
    worker_mem_kb=$(awk '/VmRSS:/ {print $2}' "/proc/$worker_main_pid/status" 2>/dev/null || echo 0)
    worker_mem_mb=$((worker_mem_kb / 1024))
    if [[ $worker_mem_mb -gt $MAX_WORKER_MEM_MB ]]; then
        systemctl kill --signal=SIGKILL "$SERVICE"
        systemctl restart "$SERVICE"
    fi
fi

age_minutes=$((MAX_AGE_SECONDS / 60))
find "$WORK_ROOT" -mindepth 1 -maxdepth 1 -type d -mmin "+$age_minutes" -exec rm -rf -- {} +
find "$META_ROOT" -mindepth 1 -maxdepth 1 -type d -mmin "+$age_minutes" -exec rm -rf -- {} +

# Clean orphaned isolate boxes through isolate itself so cgroups, quotas and
# mount points are released, then remove any leftover directory.
if [[ -d $ISOLATE_BOX_ROOT ]]; then
    while IFS= read -r box_dir; do
        box_id=${box_dir##*box-}
        if [[ $box_id =~ ^[0-9]+$ ]]; then
            "$ISOLATE_BIN" --box-id "$box_id" --cleanup >/dev/null 2>&1 || true
        fi
        rm -rf -- "$box_dir"
    done < <(find "$ISOLATE_BOX_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'box-*' -mmin "+$age_minutes")
fi