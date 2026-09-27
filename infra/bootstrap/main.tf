# One-time IAM bootstrap: three identities with least-privilege roles.
#
#   deployer  - service account for infra provisioning and deployments (Terraform, deploy.sh, CI)
#   runtime   - service account the application code runs as (attached to the VM)
#   people    - personal Google accounts: read-only console login, plus permission to
#               impersonate the deployer. No one else uses the owner account.
#
# No service account keys are created. People "act as" the deployer through impersonation
# with their own login, so every action is audited per person.

locals {
  apis = [
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # service account impersonation
    "compute.googleapis.com",
    "iap.googleapis.com",
    "oslogin.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "secretmanager.googleapis.com",
  ]

  operator_project_roles = [
    "roles/viewer",                            # see resources in the console
    "roles/logging.viewer",                    # read logs
    "roles/monitoring.viewer",                 # read metrics/dashboards
    "roles/serviceusage.serviceUsageConsumer", # needed to call APIs as the deployer
  ]
  viewer_project_roles = [
    "roles/viewer",
    "roles/logging.viewer",
    "roles/monitoring.viewer",
  ]

  operator_bindings = { for pair in setproduct(var.operators, local.operator_project_roles) : "${pair[0]} ${pair[1]}" => pair }
  viewer_bindings   = { for pair in setproduct(var.viewers, local.viewer_project_roles) : "${pair[0]} ${pair[1]}" => pair }
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- deployer

resource "google_service_account" "deployer" {
  account_id   = "${var.name}-deployer"
  display_name = "Deployer (infra provisioning and app deployment)"
  description  = "Used by Terraform, scripts/deploy.sh and CI via impersonation. No keys."
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "deployer" {
  for_each = toset(var.deployer_roles)
  project  = var.project_id
  role     = each.value
  member   = google_service_account.deployer.member
}

# The deployer may attach ONLY the runtime service account to VMs (not, for example, the default
# Compute Engine account, which has Editor). This closes the usual privilege-escalation path.
resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}

# Secrets (Secret Manager): the deployer creates and updates them (scripts/deploy.sh);
# the VM's runtime account can only READ secrets named "<secret_prefix>*".
resource "google_project_iam_member" "deployer_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = google_service_account.deployer.member
}

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_project_iam_member" "runtime_reads_app_secrets" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = google_service_account.runtime.member
  condition {
    title       = "only ${var.secret_prefix}* secrets"
    description = "The application may read its own secrets and nothing else"
    expression  = "resource.name.startsWith(\"projects/${data.google_project.this.number}/secrets/${var.secret_prefix}\")"
  }
}

# ---------------------------------------------------------------- runtime

resource "google_service_account" "runtime" {
  account_id   = "${var.name}-runtime"
  display_name = "Application runtime"
  description  = "Identity the application code uses to call Google APIs. Attached to the VM."
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "runtime" {
  for_each = toset(var.runtime_roles)
  project  = var.project_id
  role     = each.value
  member   = google_service_account.runtime.member
}

# ---------------------------------------------------------------- people

resource "google_project_iam_member" "operators" {
  for_each = local.operator_bindings
  project  = var.project_id
  member   = each.value[0]
  role     = each.value[1]
}

# Operators may impersonate the deployer (short-lived tokens only; no keys).
resource "google_service_account_iam_member" "operators_impersonate_deployer" {
  for_each           = toset(var.operators)
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value
}

resource "google_project_iam_member" "viewers" {
  for_each = local.viewer_bindings
  project  = var.project_id
  member   = each.value[0]
  role     = each.value[1]
}
