#!/bin/bash
# The daily refresh (phase 4), run by a systemd timer: fetch every source, ingest the changes,
# embed what changed, then check retrieval against the live corpus with the recorded dev
# questions. Runs in a one-off container from the image the service runs, so a deploy
# restarting the API does not interrupt it; the lock it shares with deploys keeps the two
# apart. Results go to Cloud Logging ("permitnav-refresh"); an ERROR raises the "refresh
# failed" alert (infra/monitoring.tf). The service keeps serving the last good versions.
set -euo pipefail

APP=/opt/permitnav
C=(docker compose -f docker-compose.yml -f deploy/compose.prod.yml)
MIN_R5=0.52 # the CI gate's values; the live corpus starts equal to the eval corpus
MIN_MRR=0.35

report() {
  gcloud logging write permitnav-refresh "$2" --severity="$1" --quiet || true
  echo "$2"
}
reported=0
log=""
finish() {
  local status=$?
  [ -n "$log" ] && rm -f "$log"
  if [ "$status" -ne 0 ] && [ "$reported" = 0 ]; then
    report ERROR "refresh failed unexpectedly (exit $status); see journalctl -u permitnav-refresh"
  fi
  exit "$status"
}
trap finish EXIT

cd "$APP"
exec 9>/run/permitnav-deploy.lock
flock 9 # wait for a running deploy to finish (deploys skip while this holds the lock)

log=$(mktemp)

# The daily spend cap (permitnav.spend), first, so that a failing refresh cannot hide it: a
# finished day on which questions were declined means the site's model spend reached the cap,
# which normal use never does. Usually a leaked access token. A day stays unreported until
# its error is logged, so a run that failed or never happened does not lose it.
set +e
"${C[@]}" run --rm -T api permitnav spend --unreported > "$log" 2>&1
spend_status=$?
set -e
capped=$({ grep '^SPEND CAP REACHED' "$log" || true; } | paste -sd';' -)
if [ "$spend_status" -ne 0 ]; then
  report ERROR "refresh spend check failed to run (exit $spend_status)"; tail -20 "$log"
elif [ -n "$capped" ]; then
  if gcloud logging write permitnav-refresh "refresh: $capped. If this was not you, replace API_TOKEN (infra/README.md)" --severity=ERROR --quiet; then
    echo "refresh: $capped"
    "${C[@]}" run --rm -T api permitnav spend --mark-reported > /dev/null 2>&1 \
      || report ERROR "refresh spend check: could not remember the reported days"
  else
    echo "refresh: $capped (the error could not be logged; it is sent again next run)"
  fi
fi

set +e
"${C[@]}" run --rm -T api permitnav refresh > "$log" 2>&1
refresh=$?
set -e
# grep finds nothing on most days (no changes): never let that end the script.
summary=$({ grep '^REFRESH SUMMARY:' "$log" || true; } | tail -1 | sed 's/^REFRESH SUMMARY: //')
changed=$({ grep '^  changed: ' "$log" || true; } | sed 's/^  changed: //' | head -20 | paste -sd, -)
# After an explicit ERROR report the script exits 0: the service's OnFailure unit then
# reports only kills and crashes, not a second time for a failure already reported.
if [ "$refresh" -ne 0 ]; then
  report ERROR "refresh: a step failed (${summary:-no summary}); journalctl -u permitnav-refresh has the log"
  tail -40 "$log"
  reported=1
  exit 0
fi
report INFO "refresh: ${summary:-no summary}${changed:+; units: $changed}"

# Retrieval on the live corpus: a source change can break it without any code change.
set +e
"${C[@]}" run --rm -T api permitnav eval --queries evals/queries.dev.json \
  --min-r5 "$MIN_R5" --min-mrr "$MIN_MRR" > "$log" 2>&1
eval_status=$?
set -e
result=$({ grep -E '^(gate passed|RETRIEVAL REGRESSION|GOLDEN-SET ERROR)' "$log" || true; } | head -3 | paste -sd';' -)
case "$eval_status" in
  0) report INFO "refresh eval: ${result:-passed}" ;;
  2) report ERROR "refresh eval: a golden item's expected source is gone from the corpus (drift): $result" ;;
  3) report ERROR "refresh eval: retrieval fell below the gate on the live corpus: $result" ;;
  *) report ERROR "refresh eval failed to run (exit $eval_status)"; tail -20 "$log" ;;
esac

# Golden items whose sources changed since their date (D17): they need a person's review, which
# is not a failure. Items newly drifted are a WARNING (its own alert, once per item).
set +e
"${C[@]}" run --rm -T api permitnav golden-drift > "$log" 2>&1
drift_status=$?
set -e
drift=$({ grep -E '^golden drift:' "$log" || true; } | tail -1)
if [ "$drift_status" -ne 0 ] || [ -z "$drift" ]; then
  report ERROR "refresh drift check failed to run (exit $drift_status)"; tail -20 "$log"
elif [[ "$drift" == *"(new:"* ]]; then
  # remembered as reported only once the warning is logged, so a lost alert comes back
  if gcloud logging write permitnav-refresh "refresh drift: $drift" --severity=WARNING --quiet; then
    echo "refresh drift: $drift"
    "${C[@]}" run --rm -T api permitnav golden-drift --save > /dev/null 2>&1 \
      || report ERROR "refresh drift: could not remember the reported items"
  else
    echo "refresh drift: $drift (the warning could not be logged; it is sent again next run)"
  fi
else
  report INFO "refresh drift: $drift"
fi

reported=1
