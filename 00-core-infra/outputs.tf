output "vpc_id" {
  description = "The ID of the VPC network created in core"
  value       = google_compute_network.vpc.id
}

output "subnet_id" {
  description = "The ID of the subnet created in core"
  value       = google_compute_subnetwork.subnet.id
}

output "private_vpc_connection_id" {
  description = "The ID of the private service connection created in core"
  value       = google_service_networking_connection.private_vpc_connection.id
}

output "workstation_host" {
  description = "The hostname of the workstation"
  value       = google_workstations_workstation.default.host
}
