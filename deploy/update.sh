#!/bin/bash
# Deploy on merge, pull-based (phase 3): a systemd timer runs this every 5 minutes. When
# origin/main has a new commit whose required checks passed, the VM checks it out, rebuilds
# and restarts the containers, waits for the health check, and rolls back to the previous
# commit if the new one does not become healthy.
#
# Pull, not push: GitHub needs no credentials for the VM, and nothing outside can reach in;
# the VM only reads the public repository. The ruleset on main already requires the checks;
# checking them again here guards against a push that bypassed the ruleset.
#
# Results go to Cloud Logging (log "permitnav-deploy"); an ERROR raises the "deploy failed"
# alert (infra/monitoring.tf). Every failure path reports: explicit reports below, an EXIT
# trap for anything unexpected, and the service's OnFailure unit for a killed run.
# Terraform changes (infra/) are applied by hand, not here.
set -euo pipefail

APP=/opt/permitnav
REPO=bxljoy/permitnav
REQUIRED=("Secret scan" "Lint, types and tests" "Retrieval eval (no models)")
STATE=/var/lib/permitnav
FAILED=$STATE/failed-deploy  # a commit that failed to deploy: skipped until main moves on
NOTED=$STATE/noted-check     # a commit whose check problem was already reported once
CHECK_WAIT=3600              # a required check still missing after this is reported
C=(docker compose -f docker-compose.yml -f deploy/compose.prod.yml)

report() {
  gcloud logging write permitnav-deploy "$2" --severity="$1" --quiet || true
  echo "$2"
}

# One run at a time, shared with the startup script's build (infra/startup.sh).
exec 9>/run/permitnav-deploy.lock
flock -n 9 || exit 0

# Run from a private copy: a deploy checks out new code, which may include a new version of
# this file, and bash reads a script while it runs. The lock (fd 9) is inherited.
if [ "${PERMITNAV_UPDATE_COPY:-}" != 1 ]; then
  copy=$(mktemp /run/permitnav-update.XXXXXX)
  cp "$0" "$copy"
  PERMITNAV_UPDATE_COPY=1 exec bash "$copy" "$@"
fi

reported=0
finish() {
  local status=$?
  rm -f "$0"
  if [ "$status" -ne 0 ] && [ "$reported" = 0 ]; then
    report ERROR "deploy run failed unexpectedly (exit $status); see journalctl -u permitnav-deploy"
  fi
  exit "$status"
}
trap finish EXIT
fail() { # report, then exit non-zero without a second, generic report
  report ERROR "$1"
  reported=1
  exit 1
}
note_once() { # report a check problem once per commit
  if [ "$(cat "$NOTED" 2>/dev/null)" != "$1" ]; then
    mkdir -p "$STATE" && echo "$1" > "$NOTED"
    report ERROR "$2"
  fi
  exit 0
}

cd "$APP"
git fetch -q origin main
current=$(git rev-parse HEAD)
target=$(git rev-parse origin/main)
[ "$current" = "$target" ] && exit 0
short=${target:0:7}
[ "$(cat "$FAILED" 2>/dev/null)" = "$target" ] && exit 0

# The required checks must have passed for exactly this commit.
if ! runs=$(curl -fsS "https://api.github.com/repos/$REPO/commits/$target/check-runs?per_page=100"); then
  echo "cannot read the checks for $short; trying again later"
  exit 0
fi
age=$(($(date +%s) - $(git show -s --format=%ct "$target")))
for check in "${REQUIRED[@]}"; do
  conclusion=$(printf '%s' "$runs" | python3 -c '
import json, sys
name = sys.argv[1]
runs = [r for r in json.load(sys.stdin).get("check_runs", []) if r["name"] == name]
print("missing" if not runs else runs[0]["conclusion"] if runs[0]["status"] == "completed" else "pending")
' "$check")
  case "$conclusion" in
    success) ;;
    pending) exit 0 ;; # still running
    missing)
      [ "$age" -lt "$CHECK_WAIT" ] && exit 0 # not started yet
      note_once "$target" "deploy of $short waiting: required check '$check' has not run after an hour (renamed job?)"
      ;;
    *) # failure, cancelled, …: not deployed; a re-run that passes is deployed then
      note_once "$target" "deploy of $short skipped: required check '$check' is $conclusion"
      ;;
  esac
done

healthy() {
  for _ in $(seq 1 60); do # up to 5 minutes: a first start loads the embedding model
    [ "$(docker inspect -f '{{.State.Health.Status}}' permitnav-api-1 2>/dev/null)" = healthy ] && return 0
    sleep 5
  done
  return 1
}

deploy() { # check out a commit and start it; each step checked (set -e is off inside `if`)
  git -c advice.detachedHead=false checkout -q "$1" || return 1
  "${C[@]}" up -d --build --remove-orphans || return 1
  if ! git diff --quiet "$2" "$1" -- deploy/Caddyfile; then
    # A single-file bind mount keeps the old file after checkout: recreate Caddy.
    "${C[@]}" up -d --force-recreate caddy || return 1
  fi
}

migrations_changed=0
git diff --quiet "$current" "$target" -- migrations/ || migrations_changed=1
if [ "$migrations_changed" = 1 ]; then
  # The new code migrates the database on start; take a backup first.
  BACKUP_BUCKET=$(sed -n 's/^BACKUP_BUCKET=//p' /etc/permitnav-backup.env) \
    "$APP/deploy/backup.sh" || fail "deploy of $short stopped: the pre-migration backup failed"
fi

if deploy "$target" "$current" && healthy; then
  docker image prune -f >/dev/null || true
  rm -f "$FAILED"
  report INFO "deployed $short (was ${current:0:7})"
  exit 0
fi

mkdir -p "$STATE" && echo "$target" > "$FAILED"
if [ "$migrations_changed" = 1 ]; then
  # Old code on a migrated schema may look healthy and still fail: no automatic rollback.
  fail "deploy of $short failed after migrating the database; NOT rolled back. Fix forward, or restore the pre-migration backup and check out ${current:0:7} (infra/README.md)"
fi
if deploy "$current" "$target" && healthy; then
  fail "deploy of $short failed; rolled back to ${current:0:7}"
fi
fail "deploy of $short failed and the rollback to ${current:0:7} is not healthy"
