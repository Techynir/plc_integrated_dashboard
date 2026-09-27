output "deployer_service_account" {
  description = "Set as deployer_service_account in infra/terraform/terraform.tfvars"
  value       = google_service_account.deployer.email
}

output "runtime_service_account" {
  description = "Set as runtime_service_account in infra/terraform/terraform.tfvars"
  value       = google_service_account.runtime.email
}

output "secret_prefix" {
  value = var.secret_prefix
}
