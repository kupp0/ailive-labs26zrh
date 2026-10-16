variable "project_id" {
  description = "The GCP project designated role=admin in projects.txt"
  type        = string
}

variable "region" {
  description = "The admin project's region from projects.txt"
  type        = string
  default     = "europe-west3"
}
