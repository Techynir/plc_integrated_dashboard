variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "deployer_service_account" {
  description = "Service account Terraform acts as (output of infra/bootstrap). Empty = your own credentials."
  type        = string
  default     = ""
}

variable "runtime_service_account" {
  description = "Service account attached to the VM for application code (output of infra/bootstrap)"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "asia-south1" # Mumbai
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "asia-south1-a"
}

variable "name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "plc-dashboard"
}

variable "machine_type" {
  description = "e2-small (2 GB) is enough for a demo; use e2-medium for a sustained 50 msg/s."
  type        = string
  default     = "e2-small"
}

variable "disk_size_gb" {
  description = "Boot disk (OS + Docker images + TimescaleDB data)"
  type        = number
  default     = 30
}

variable "mqtt_source_ranges" {
  description = "CIDRs allowed to reach MQTT/TLS on 8883. Narrow to the plant's public IPs when known."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "web_source_ranges" {
  description = "CIDRs allowed to reach the dashboard on 80/443"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "snapshot_retention_days" {
  description = "Daily disk snapshots are kept this many days"
  type        = number
  default     = 7
}
