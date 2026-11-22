variable "project_id" {
  description = "The Google Cloud project ID (e.g. permitnav-123456)."
  type        = string
}

variable "region" {
  description = "An EU region (D15). Stockholm; europe-north1 (Finland) if unavailable."
  type        = string
  default     = "europe-north2"
}

variable "zone" {
  type    = string
  default = "europe-north2-a"
}

variable "machine_type" {
  description = "2 vCPU, 8 GB: bge-m3 on the CPU (~1.1 GB measured) next to Postgres."
  type        = string
  default     = "e2-standard-2"
}

variable "disk_gb" {
  description = "Boot disk: Docker images (~3.3 GB for the API), the database, raw downloads."
  type        = number
  default     = 30
}

variable "backup_location" {
  description = "Where database backups are kept: in the EU (D15)."
  type        = string
  default     = "EU"
}

variable "backup_retention_days" {
  type    = number
  default = 30
}

variable "repo_url" {
  description = "The public repository the VM clones (the code only; secrets stay on the VM)."
  type        = string
  default     = "https://github.com/bxljoy/permitnav.git"
}
