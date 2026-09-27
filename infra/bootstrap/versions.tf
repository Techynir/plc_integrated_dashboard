terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

# Runs with the project OWNER's own credentials (gcloud auth application-default login),
# once, and again only when access changes. Everything else runs as the deployer.
provider "google" {
  project = var.project_id
}
