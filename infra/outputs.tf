output "ip_address" {
  description = "Point the DNS A record (DNS only, no proxy) for the service's domain here."
  value       = google_compute_address.web.address
}

output "ssh" {
  description = "SSH through Google's IAP tunnel (no SSH port is open to the internet)."
  value       = "gcloud compute ssh ${google_compute_instance.app.name} --zone ${var.zone} --tunnel-through-iap --project ${var.project_id}"
}

output "backup_bucket" {
  value = google_storage_bucket.backups.name
}
