# Engine contract: the engine passes ONLY project_id and region. Everything
# else here must have a default.

variable "project_id" {
  description = "The role=admin project that hosts the shared reservation."
  type        = string
}

variable "region" {
  description = "The admin project's region from projects.txt. Unused: the reservation lives in the US multi-region to match Lab 10's datasets."
  type        = string
}

variable "max_slots" {
  description = "Autoscale ceiling for the cohort-wide graph-lab reservation. Shared by every participant; must be a positive multiple of 100."
  type        = number
  default     = 500

  validation {
    condition     = var.max_slots % 100 == 0 && var.max_slots > 0
    error_message = "max_slots must be a positive multiple of 100."
  }
}
