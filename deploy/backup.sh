#!/bin/bash
# Nightly database backup (systemd timer from infra/startup.sh): a compressed pg_dump of the
# running database, uploaded to the EU bucket in BACKUP_BUCKET. The bucket keeps versions
# and deletes backups after 30 days. Restore: infra/README.md.
set -euo pipefail

# The result goes to Cloud Logging (log "permitnav-backup"): an ERROR raises the "backup
# failed" alert (infra/monitoring.tf), an INFO records each good run.
report() {
  gcloud logging write permitnav-backup "$2" --severity="$1" --quiet || true
}
tmp=""
finish() {
  local status=$?
  [ -n "$tmp" ] && rm -f "$tmp"
  # Every non-zero exit, including a missing variable (which skips an ERR trap). A kill that
  # skips this too is reported by the OnFailure unit (infra/startup.sh).
  [ "$status" -ne 0 ] && report ERROR "backup failed (exit $status)"
  exit "$status"
}
trap finish EXIT

: "${BACKUP_BUCKET:?BACKUP_BUCKET is not set}"
cd /opt/permitnav
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp=$(mktemp)

docker compose -f docker-compose.yml -f deploy/compose.prod.yml exec -T db \
  pg_dump -U permitnav -d permitnav --format=custom --compress=9 > "$tmp"
# One-piece upload: a parallel composite upload (gcloud's choice for large files) must delete
# its temporary pieces, which the VM's object-creator role cannot do, by design.
CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED=False \
  gcloud storage cp "$tmp" "gs://$BACKUP_BUCKET/db/permitnav-$stamp.dump" --quiet
message="backup permitnav-$stamp.dump: $(stat -c %s "$tmp") bytes"
echo "$message"
report INFO "$message"
