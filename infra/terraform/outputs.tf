output "project_id" {
  value = var.project_id
}

output "deployer_service_account" {
  value = var.deployer_service_account
}

output "zone" {
  value = var.zone
}

output "instance_name" {
  value = google_compute_instance.vm.name
}

output "external_ip" {
  value = google_compute_address.ip.address
}

output "default_hostname" {
  description = "Resolves to the static IP without any DNS setup (used for HTTPS and the MQTT certificate)"
  value       = "${replace(google_compute_address.ip.address, ".", "-")}.sslip.io"
}

output "dashboard_url" {
  value = "https://${replace(google_compute_address.ip.address, ".", "-")}.sslip.io"
}
