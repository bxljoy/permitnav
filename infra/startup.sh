#!/bin/bash
# Runs on every boot of the VM (Compute Engine startup script); safe to run again.
# Installs Docker, clones the code, installs the nightly backup, and starts PermitNav once
# its secrets file exists. Secrets never pass through here: /opt/permitnav/.env is written by
# hand over SSH (infra/README.md). Terraform fills in repo_url and backup_bucket.
set -euo pipefail

APP=/opt/permitnav

if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
fi
command -v gcloud >/dev/null || apt-get install -y google-cloud-cli

# Security updates install themselves daily; a reboot, when one is needed, at 04:30 UTC
# (after the 03:30 backup). Docker restarts the containers.
if [ ! -f /etc/apt/apt.conf.d/52permitnav-upgrades ]; then
  apt-get install -y unattended-upgrades
  cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
  cat > /etc/apt/apt.conf.d/52permitnav-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF
fi

if [ ! -d "$APP/.git" ]; then
  git clone "${repo_url}" "$APP"
fi

# Nightly database backup to the EU bucket (03:30 UTC), kept 30 days by the bucket's rule.
cat > /etc/permitnav-backup.env <<EOF
BACKUP_BUCKET=${backup_bucket}
EOF
cat > /etc/systemd/system/permitnav-backup-failed.service <<'EOF'
[Unit]
Description=Report a failed PermitNav backup (raises the "backup failed" alert)
[Service]
Type=oneshot
ExecStart=/usr/bin/gcloud logging write permitnav-backup "backup service failed (killed or crashed)" --severity=ERROR --quiet
EOF
cat > /etc/systemd/system/permitnav-backup.service <<'EOF'
[Unit]
Description=PermitNav database backup
OnFailure=permitnav-backup-failed.service
[Service]
Type=oneshot
EnvironmentFile=/etc/permitnav-backup.env
ExecStart=/opt/permitnav/deploy/backup.sh
EOF
cat > /etc/systemd/system/permitnav-backup.timer <<'EOF'
[Unit]
Description=Nightly PermitNav database backup
[Timer]
OnCalendar=*-*-* 03:30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now permitnav-backup.timer

# Start (or restart) the service only once the secrets exist.
if [ -f "$APP/.env" ]; then
  cd "$APP"
  docker compose -f docker-compose.yml -f deploy/compose.prod.yml up -d --build
  docker image prune -f  # each rebuild leaves the previous ~3.3 GB image behind
fi
