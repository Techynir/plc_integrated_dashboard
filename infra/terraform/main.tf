# Demo profile: a single Compute Engine VM running the whole stack with Docker Compose.
# See docs/REQUIREMENTS.md §5.1 and §9.

# APIs, service accounts and IAM live in infra/bootstrap (applied once by the project owner).
# This configuration runs as the deployer service account, which cannot change IAM.

resource "google_compute_address" "ip" {
  name   = "${var.name}-ip"
  region = var.region
}

resource "google_compute_firewall" "web" {
  name          = "${var.name}-allow-web"
  network       = "default"
  direction     = "INGRESS"
  source_ranges = var.web_source_ranges
  target_tags   = [var.name]
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
}

resource "google_compute_firewall" "mqtt" {
  name          = "${var.name}-allow-mqtt-tls"
  network       = "default"
  direction     = "INGRESS"
  source_ranges = var.mqtt_source_ranges
  target_tags   = [var.name]
  allow {
    protocol = "tcp"
    ports    = ["8883"]
  }
}

# SSH only through Identity-Aware Proxy TCP forwarding (gcloud compute ssh --tunnel-through-iap).
resource "google_compute_firewall" "iap_ssh" {
  name          = "${var.name}-allow-iap-ssh"
  network       = "default"
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [var.name]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_resource_policy" "daily_snapshot" {
  name   = "${var.name}-daily-snapshot"
  region = var.region
  snapshot_schedule_policy {
    schedule {
      daily_schedule {
        days_in_cycle = 1
        start_time    = "21:00" # UTC = 02:30 IST (GCP requires whole hours)
      }
    }
    retention_policy {
      max_retention_days    = var.snapshot_retention_days
      on_source_disk_delete = "KEEP_AUTO_SNAPSHOTS"
    }
    snapshot_properties {
      storage_locations = [var.region]
    }
  }
}

resource "google_compute_instance" "vm" {
  name         = var.name
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [var.name]

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = var.disk_size_gb
      type  = "pd-balanced"
    }
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.ip.address
    }
  }

  # Application code on the VM calls Google APIs as the runtime service account;
  # what it may do is controlled by that account's IAM roles (infra/bootstrap).
  service_account {
    email  = var.runtime_service_account
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin = "TRUE"
    startup-script = file("${path.module}/startup.sh")
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  allow_stopping_for_update = true
}

resource "google_compute_disk_resource_policy_attachment" "snapshots" {
  name = google_compute_resource_policy.daily_snapshot.name
  disk = google_compute_instance.vm.name
  zone = var.zone
}
