#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run as root: sudo bash deploy/linux/backup.sh" >&2
    exit 1
fi

INFRA_ENV=/etc/xinbaoyan/infra.env
INFRA_DIR=/opt/xinbaoyan/infra
BACKUP_ROOT=${BACKUP_ROOT:-/var/backups/xinbaoyan}
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-14}
if [[ ! $RETENTION_DAYS =~ ^[0-9]+$ ]] || [[ $RETENTION_DAYS -lt 1 ]]; then
    echo "BACKUP_RETENTION_DAYS must be a positive integer, got: $RETENTION_DAYS" >&2
    exit 1
fi
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)

[[ -r $INFRA_ENV ]] || { echo "Missing $INFRA_ENV" >&2; exit 1; }
set -a
source "$INFRA_ENV"
set +a
: "${MYSQL_DATABASE:?}" "${MYSQL_ROOT_PASSWORD:?}"

install -d -m 700 "$BACKUP_ROOT"
exec 9>"$BACKUP_ROOT/.backup.lock"
flock -n 9 || { echo "Another backup is already running." >&2; exit 1; }

TEMP_DIR=$(mktemp -d "$BACKUP_ROOT/.tmp.$TIMESTAMP.XXXXXX")
FINAL_DIR="$BACKUP_ROOT/$TIMESTAMP"
cleanup() { rm -rf -- "$TEMP_DIR"; }
trap cleanup EXIT

COMPOSE=(docker compose --env-file "$INFRA_ENV" -f "$INFRA_DIR/docker-compose.yml")

if [[ ${INFRA_MODE:-external} == external ]]; then
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump --host=127.0.0.1 --port=3306 --protocol=TCP \
        -uroot --single-transaction --quick --routines --triggers --events \
        --set-gtid-purged=OFF --databases "$MYSQL_DATABASE" | gzip -9 > "$TEMP_DIR/mysql.sql.gz"
else
    # Pass the password through the container's own environment, not through
    # docker CLI argv (-e KEY=VALUE is visible in `ps` to every local user).
    "${COMPOSE[@]}" exec -T mysql sh -c \
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot --single-transaction --quick --routines --triggers --events --set-gtid-purged=OFF --databases "$MYSQL_DATABASE"' \
        | gzip -9 > "$TEMP_DIR/mysql.sql.gz"
fi

tar -C /var/lib/xinbaoyan -czf "$TEMP_DIR/storage.tar.gz" data

# Verify the artifacts before they become the migration safety net: a
# truncated gzip or a checksum mismatch must fail the backup, not silently
# produce an unrestorable file.
gzip -t "$TEMP_DIR/mysql.sql.gz"
gzip -t "$TEMP_DIR/storage.tar.gz"
(
    cd "$TEMP_DIR"
    sha256sum mysql.sql.gz storage.tar.gz > SHA256SUMS
    sha256sum -c SHA256SUMS
)
chmod 600 "$TEMP_DIR"/*
mv "$TEMP_DIR" "$FINAL_DIR"
trap - EXIT

# Optional offsite copy (configure BACKUP_RCLONE_REMOTE, e.g. remote:bucket/path).
# A remote failure warns but does not invalidate the verified local backup.
if [[ -n ${BACKUP_RCLONE_REMOTE:-} ]]; then
    if command -v rclone >/dev/null 2>&1; then
        if rclone copy "$FINAL_DIR" "$BACKUP_RCLONE_REMOTE/$TIMESTAMP"; then
            echo "Offsite copy complete: $BACKUP_RCLONE_REMOTE/$TIMESTAMP"
        else
            echo "WARNING: offsite copy to $BACKUP_RCLONE_REMOTE failed; local backup is intact." >&2
        fi
    else
        echo "WARNING: BACKUP_RCLONE_REMOTE is set but rclone is not installed." >&2
    fi
fi

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' \
    -mtime "+$RETENTION_DAYS" -exec rm -rf -- {} +

echo "Backup complete (verified): $FINAL_DIR"
