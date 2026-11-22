# Monitoring (phase 3): an uptime check on /health and an alert on any failed nightly
# backup, by email. Cloud Monitoring and Logging, within their free allowances at this size.
#
# No "backup missing" alert: a metric-absence window is shorter than a day, so it would fire
# before every nightly run. A backup that never runs because the VM is down is caught by the
# uptime check; one that runs and fails writes an ERROR entry, caught below.

resource "google_project_service" "monitoring" {
  for_each = toset([
    "monitoring.googleapis.com",
    "logging.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "PermitNav alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  depends_on = [google_project_service.monitoring]
}

# The VM writes the backup's result to Cloud Logging (and nothing else to the project).
resource "google_project_iam_member" "vm_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# --- Is the service up? ----------------------------------------------------------------------

resource "google_monitoring_uptime_check_config" "health" {
  display_name = "PermitNav /health"
  timeout      = "10s"
  period       = "300s"

  http_check {
    path         = "/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
    accepted_response_status_codes {
      status_value = 200
    }
  }
  content_matchers {
    content = "\"ok\""
    matcher = "CONTAINS_STRING"
  }
  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = var.domain
    }
  }
  depends_on = [google_project_service.monitoring]
}

resource "google_monitoring_alert_policy" "down" {
  display_name = "PermitNav is down"
  combiner     = "OR"
  conditions {
    display_name = "/health fails from 2 or more checker regions"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.health.uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"
      aggregations {
        alignment_period     = "1200s" # Google's uptime-alert recipe: every region has a point
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.host"]
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
  documentation {
    content   = "https://${var.domain}/health is failing. Runbook: infra/README.md (Operate). Check `docker compose ps` and the api/caddy logs on the VM."
    mime_type = "text/markdown"
  }
}

# --- Did the nightly backup fail? ------------------------------------------------------------

resource "google_monitoring_alert_policy" "backup_failed" {
  display_name = "PermitNav backup failed"
  combiner     = "OR"
  conditions {
    display_name = "permitnav-backup wrote an ERROR"
    condition_matched_log {
      filter = "logName=\"projects/${var.project_id}/logs/permitnav-backup\" AND severity>=ERROR"
    }
  }
  alert_strategy {
    notification_rate_limit {
      period = "3600s"
    }
    auto_close = "86400s"
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
  documentation {
    content   = "The nightly database backup failed. On the VM: `sudo journalctl -u permitnav-backup.service`. Runbook: infra/README.md."
    mime_type = "text/markdown"
  }
  depends_on = [google_project_service.monitoring]
}
