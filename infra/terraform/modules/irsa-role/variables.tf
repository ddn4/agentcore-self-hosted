variable "name" {
  description = "IAM role name (runbook 4.1 / 7b.3)."
  type        = string
}

variable "description" {
  description = "IAM role description."
  type        = string
}

variable "oidc_provider_arn" {
  description = "The EKS cluster's IAM OIDC provider ARN (module.eks.oidc_provider_arn)."
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace of the one ServiceAccount allowed to assume the role."
  type        = string
}

variable "service_account" {
  description = "Kubernetes ServiceAccount name allowed to assume the role."
  type        = string
}

variable "policy_json" {
  description = "Inline policy document (JSON) with the role's permissions."
  type        = string
}

variable "max_session_duration" {
  description = "Maximum session duration in seconds."
  type        = number
  default     = 3600
}
