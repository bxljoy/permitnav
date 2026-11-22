# Deploying PermitNav (phase 3)

One VM in an EU region on Google Cloud, running the same Docker Compose setup as local
development, with Caddy for HTTPS. Terraform creates the VM, a static IP, the firewall
(80/443 open; SSH only through Google's IAP tunnel), the VM's service account and an EU
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
   DOMAIN=<the subdomain>
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
- **Update:** `sudo git pull && sudo $C up -d --build && sudo docker image prune -f`
  (deploy on merge automates this later).
- **Backups:** nightly at 03:30 UTC (`systemctl list-timers permitnav-backup.timer`), kept
  30 days. The VM can add backups but not delete or overwrite them. Restore:
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
