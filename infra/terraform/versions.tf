terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # For team use, keep state in a GCS bucket:
  # backend "gcs" { bucket = "<project>-tfstate", prefix = "plc-dashboard" }
}

# Authenticates with your own login (gcloud auth application-default login) and then acts
# as the deployer service account. You need roles/iam.serviceAccountTokenCreator on it
# (granted to "operators" in infra/bootstrap).
provider "google" {
  project                     = var.project_id
  region                      = var.region
  zone                        = var.zone
  impersonate_service_account = var.deployer_service_account != "" ? var.deployer_service_account : null
}
