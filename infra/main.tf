# PermitNav on Google Cloud (phase 3): one small VM in an EU region running the same Docker
# Compose setup as local development, with Caddy for HTTPS, plus an EU bucket for nightly
# database backups. Nothing else: no load balancer, no managed database, no serverless.
#
#   terraform init
#   terraform plan  -var-file=permitnav.tfvars    # review first
#   terraform apply -var-file=permitnav.tfvars
#
# State stays local (git-ignored) for a single-owner project; see infra/README.md.

terraform {
  required_version = ">= 1.9"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

locals {
  name   = "permitnav"
  labels = { app = "permitnav", managed-by = "terraform" }
}

resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iap.googleapis.com", # SSH through Google's IAP tunnel: no SSH port open to the internet
    "storage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --- Network ---------------------------------------------------------------------------------

resource "google_compute_address" "web" {
  name       = "${local.name}-ip"
  region     = var.region
  depends_on = [google_project_service.apis]
}

resource "google_compute_firewall" "web" {
  name          = "${local.name}-allow-web"
  network       = "default"
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${local.name}-web"]
  allow {
    protocol = "tcp"
    ports    = ["80", "443"] # Caddy: HTTP only to redirect and obtain certificates
  }
  depends_on = [google_project_service.apis]
}

resource "google_compute_firewall" "iap_ssh" {
  name          = "${local.name}-allow-iap-ssh"
  network       = "default"
  direction     = "INGRESS"
  priority      = 900                 # before the deny below
  source_ranges = ["35.235.240.0/20"] # Google's IAP TCP forwarding range, nothing else
  target_tags   = ["${local.name}-web"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  depends_on = [google_project_service.apis]
}

# The default network allows SSH and RDP from anywhere (default-allow-ssh/-rdp, priority
# 65534). Those rules belong to the project, not to this module, so they are outranked here
# for this VM instead of deleted: SSH only through IAP (above), RDP never.
resource "google_compute_firewall" "deny_ssh_rdp" {
  name          = "${local.name}-deny-ssh-rdp"
  network       = "default"
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["${local.name}-web"]
  deny {
    protocol = "tcp"
    ports    = ["22", "3389"]
  }
  # The IAP allow rule must have its higher priority before this deny exists.
  depends_on = [google_project_service.apis, google_compute_firewall.iap_ssh]
}

# --- Backups ---------------------------------------------------------------------------------

resource "google_storage_bucket" "backups" {
  name                        = "${var.project_id}-${local.name}-backups"
  location                    = var.backup_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      age = var.backup_retention_days
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

# The VM's own identity: on that bucket it may create and read backups, and nothing else.
# Object creator and viewer, not admin: a compromised VM cannot delete or overwrite backups.
resource "google_service_account" "vm" {
  account_id   = "${local.name}-vm"
  display_name = "PermitNav VM"
  depends_on   = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "vm_backups" {
  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.vm.email}"
}

# Read and list too: `gcloud storage cp` reads the destination before writing (measured: 403
# on storage.objects.get without it), and the restore runbook lists the bucket to find the
# latest dump. The dumps hold what the VM already holds (the live database: the public corpus
# today); deleting and overwriting stay impossible (no storage.objects.delete, verified).
resource "google_storage_bucket_iam_member" "vm_backups_read" {
  bucket = google_storage_bucket.backups.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.vm.email}"
}

# --- The VM ----------------------------------------------------------------------------------

resource "google_compute_instance" "app" {
  name         = local.name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = ["${local.name}-web"]
  labels       = local.labels

  boot_disk {
    initialize_params {
      image  = "debian-cloud/debian-12"
      size   = var.disk_gb
      type   = "pd-balanced"
      labels = local.labels
    }
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.web.address
    }
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"] # narrowed by the account's IAM: the backup bucket, writing logs
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin = "TRUE" # SSH keys through Google accounts, no keys in metadata
    startup-script = templatefile("${path.module}/startup.sh", {
      repo_url      = var.repo_url
      backup_bucket = google_storage_bucket.backups.name
    })
  }

  allow_stopping_for_update = true
  depends_on                = [google_project_service.apis]
}
