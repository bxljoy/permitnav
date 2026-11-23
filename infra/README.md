# Deploying PermitNav (phase 3)

One VM in an EU region on Google Cloud, running the same Docker Compose setup as local
development, with Caddy for HTTPS. Terraform creates the VM, a static IP, the firewall
(80/443 open; SSH only through Google's IAP tunnel; GCP's default allow-SSH and allow-RDP
rules outranked by a deny rule for this VM), the VM's service account and an EU
bucket for nightly database backups. Secrets live only in `/opt/permitnav/.env` on the VM.

## Create

```
gcloud auth login && gcloud auth application-default login
cd infra
cp example.tfvars permitnav.tfvars     # set project_id (git-ignored)
terraform init
terraform plan  -var-file=permitnav.tfvars
terraform apply -var-file=permitnav.tfvars
```

Then:

1. **DNS:** an `A` record for the service's subdomain pointing at the `ip_address` output,
   **DNS only** (no proxy: the proxy would see every question in plain text).
2. **Secrets:** open a shell (`ssh` output) and write `/opt/permitnav/.env` (mode 600,
   owned by root):
   ```
   DOMAIN=<the subdomain; the same as `domain` in permitnav.tfvars>
   API_TOKEN=<a new random token, not the local one>
   POSTGRES_PASSWORD=<a new random password; the repo's is for local development only>
   OPENROUTER_API_KEY=<a server-only key with its own monthly credit limit>
   ANSWER_MODEL=anthropic/claude-haiku-5.5
   LANGFUSE_PUBLIC_KEY=...
   LANGFUSE_SECRET_KEY=...
   ```
3. **Start:** `sudo google_metadata_script_runner startup` (or reboot) builds and starts the
   containers.
4. **Corpus:** load the published eval corpus (enough to answer; phase 4 adds re-crawling):
   ```
   cd /opt/permitnav
   C="docker compose -f docker-compose.yml -f deploy/compose.prod.yml"
   sudo curl -fsSLO https://github.com/bxljoy/permitnav/releases/download/eval-corpus-2026-11-22/eval-corpus.txt.gz
   sudo $C cp eval-corpus.txt.gz api:/tmp/
   sudo $C exec api permitnav import-eval-corpus /tmp/eval-corpus.txt.gz
   ```
5. **Check:** `curl https://<subdomain>/health`, then a question with the token.

## Operate

On the VM, every command starts from:

```
cd /opt/permitnav
C="docker compose -f docker-compose.yml -f deploy/compose.prod.yml"
```

- **Logs:** `sudo $C logs -f api caddy` (Caddy's access logs: path, status, timing; no bodies).
- **Updates deploy themselves:** every 5 minutes the VM checks `main`; for a new commit whose
  required checks passed, it pulls the API image CI built (`ghcr.io/bxljoy/permitnav-api`,
  run by digest; the VM never builds) and starts it; it is rolled back if it does not become healthy
  (`deploy/update.sh`; `journalctl -u permitnav-deploy.service`; results in the
  `permitnav-deploy` log). A failed commit is skipped until a newer one arrives. To deploy now:
  `sudo systemctl start permitnav-deploy.service`. Changes to `infra/` (Terraform, the startup
  script) still need `terraform apply`, and the startup script a reboot or
  `sudo google_metadata_script_runner startup`.
- **The image package is public:** the VM pulls without credentials. GitHub made the package
  public on its first push because the image's `org.opencontainers.image.source` label links it
  to this public repository (checked: an anonymous pull works). If a pull ever fails with
  "unauthorized", check the package's visibility (profile → Packages → `permitnav-api`).
- **Rolling back by hand** (e.g. after a failed deploy that migrated the database): restore the
  backup if needed, `sudo git checkout <commit>`, set `PERMITNAV_IMAGE=<that commit's image
  digest>` in `.env` (the alert names it; empty means build from the checkout), then
  `sudo $C up -d`.
- **Daily refresh (02:30 UTC):** fetches every source (statutes, Migrationsverket, rulings, the
  amendment register and the SFS feed), ingests what changed, dates undated wordings from the
  register, embeds what changed, then
  checks retrieval on the live corpus (`deploy/refresh.sh`; `journalctl -u permitnav-refresh`;
  results in the `permitnav-refresh` log, failures raise "PermitNav refresh failed"). What
  changed is in the `change_log` table:
  `sudo $C exec db psql -U permitnav -c "SELECT observed_at, kind, unit_id FROM change_log ORDER BY id DESC LIMIT 20"`.
  To run it now: `sudo systemctl start permitnav-refresh.service`.
- **Backups:** nightly at 03:30 UTC (`systemctl list-timers permitnav-backup.timer`), kept
  30 days. The VM can create, list and read backups, but not delete or overwrite them. Restore
  on the VM:
  ```
  gcloud storage cp gs://<bucket>/db/<file>.dump /tmp/restore.dump
  sudo $C exec -T db pg_restore --clean --if-exists -U permitnav -d permitnav < /tmp/restore.dump
  ```
- **Security updates** install daily by themselves; a reboot, when needed, at 04:30 UTC.

## Costs and the free trial

`e2-standard-2` in Stockholm is about $55 a month plus a few dollars for the disk, IP and
bucket: roughly half of the $300 trial credit over its 90 days. The trial credit does not
cover Vertex AI or partner models; the answer model runs through OpenRouter. Before the
trial ends (90 days), either upgrade the billing account or move: the setup is Docker
Compose, so moving means a database dump, a new VM and a DNS change.

## Tear down

`terraform destroy -var-file=permitnav.tfvars`. The backup bucket is kept unless emptied
first (`force_destroy = false`), so a destroy never deletes the backups by accident.
