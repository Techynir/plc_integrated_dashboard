variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "name" {
  description = "Prefix for service account IDs (6-30 chars total, lowercase)"
  type        = string
  default     = "plc"
}

variable "operators" {
  description = <<-EOT
    People who deploy and provision (they impersonate the deployer service account).
    Format: "user:alice@example.com" or "group:platform-team@example.com".
  EOT
  type        = list(string)
  default     = []
}

variable "viewers" {
  description = "People who may log in to the Cloud Console read-only (resources, logs, metrics)."
  type        = list(string)
  default     = []
}

variable "deployer_roles" {
  description = <<-EOT
    Project roles for the deployer service account. Deliberately excludes Owner/Editor and any
    IAM-admin role, so the deployer cannot grant itself or others more access.
  EOT
  type        = list(string)
  default = [
    "roles/compute.admin",                     # VMs, disks, static IPs, firewall rules, snapshot schedules
    "roles/iap.tunnelResourceAccessor",        # SSH/SCP to the VM through IAP (no public SSH port)
    "roles/compute.osAdminLogin",              # sudo on the VM via OS Login (docker compose)
    "roles/serviceusage.serviceUsageConsumer", # bill API calls to this project
    # Add when moving to the production profile (REQUIREMENTS.md §5.4), e.g.:
    # "roles/run.admin", "roles/artifactregistry.admin", "roles/pubsub.admin",
    # "roles/bigquery.admin", "roles/storage.admin", "roles/secretmanager.admin",
  ]
}

variable "runtime_roles" {
  description = <<-EOT
    Project roles for the application runtime service account, the identity application code
    uses when it calls Google APIs. Grant only what the code calls.
  EOT
  type        = list(string)
  default = [
    "roles/logging.logWriter",
    "roles/logging.viewer", # Admin -> Logs page reads the application logs
    "roles/monitoring.metricWriter",
    # Examples, add as the code needs them:
    # "roles/secretmanager.secretAccessor", "roles/pubsub.publisher", "roles/pubsub.subscriber",
    # "roles/storage.objectUser", "roles/bigquery.dataEditor", "roles/cloudsql.client",
  ]
}

variable "secret_prefix" {
  description = "Secret Manager secrets whose names start with this prefix are readable by the application VM"
  type        = string
  default     = "plc-"
}
