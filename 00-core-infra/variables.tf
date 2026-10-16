variable "project_id" {
  description = "The target ephemeral GCP project allocated for the participant"
  type        = string
}

variable "region" {
  description = "The target deployment region"
  type        = string
  default     = "europe-west3"
}

variable "network_name" {
  description = "The name of the VPC network"
  type        = string
  default     = "lab-vpc"
}

variable "subnet_name" {
  description = "The name of the subnet"
  type        = string
  default     = "lab-subnet"
}

variable "subnet_cidr" {
  description = "The CIDR range for the subnet"
  type        = string
  default     = "10.0.1.0/24"
}

variable "iap_member" {
  description = "The IAM member string for the participant (e.g., user:devstar@gcplab.me)"
  type        = string
}
