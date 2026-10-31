variable "region" {
  description = "The only Region this project uses."
  type        = string
  default     = "us-east-1"
}

variable "network_state_path" {
  description = "Local state of 10-network (terraform_remote_state)."
  type        = string
  default     = "../10-network/terraform.tfstate"
}

# --- EKS -------------------------------------------------------------------------------

variable "cluster_name" {
  description = "EKS cluster name (plan v0.10: aws eks update-kubeconfig --name temporal)."
  type        = string
  default     = "temporal"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version: the standard-support default on 2026-10-31, same minor as the kind node image (v0.9)."
  type        = string
  default     = "1.37"
}

variable "public_access_cidrs" {
  description = "CIDRs allowed on the public API endpoint: the laptop's public IP /32 only. Set in the gitignored terraform.auto.tfvars (never commit it)."
  type        = list(string)
  sensitive   = true

  validation {
    condition     = length(var.public_access_cidrs) > 0 && alltrue([for c in var.public_access_cidrs : endswith(c, "/32")])
    error_message = "public_access_cidrs must be one or more /32 addresses (the laptop's public IP)."
  }
}

variable "node_instance_type" {
  description = "Node instance type (ADR-0006: m7g.large arm64; fallback m6i.xlarge with node_ami_type AL2023_x86_64_STANDARD and amd64 images)."
  type        = string
  default     = "m7g.large"
}

variable "node_ami_type" {
  description = "EKS managed node group AMI type matching node_instance_type."
  type        = string
  default     = "AL2023_ARM_64_STANDARD"
}

variable "node_min_size" {
  description = "Node group minimum (make pause sets 0 outside Terraform, make resume restores this)."
  type        = number
  default     = 2
}

variable "node_desired_size" {
  description = "Node group desired size at creation (the module ignores later changes)."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Node group maximum."
  type        = number
  default     = 3
}

variable "control_plane_log_retention_days" {
  description = "Retention of /aws/eks/<cluster>/cluster."
  type        = number
  default     = 7
}

variable "lbc_chart_version" {
  description = "aws-load-balancer-controller chart (https://aws.github.io/eks-charts)."
  type        = string
  default     = "3.6.2"
}

variable "cert_manager_chart_version" {
  description = "cert-manager chart (https://charts.jetstack.io); same as kind (v0.9)."
  type        = string
  default     = "v1.21.3"
}

# --- RDS -------------------------------------------------------------------------------

variable "db_identifier" {
  description = "RDS instance identifier."
  type        = string
  default     = "temporal-db"
}

variable "db_engine_version" {
  description = "PostgreSQL minor. Temporal tests 16.6, which RDS no longer offers (2026-10-31: 16.9-16.15); see docs/versions.md."
  type        = string
  default     = "16.15"
}

variable "db_instance_class" {
  description = "ADR-0006: db.t4g.medium single-AZ vs the runbook's db.r6g.large Multi-AZ."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_allocated_storage" {
  description = "gp3 storage in GiB (20 is the gp3 minimum)."
  type        = number
  default     = 20
}

variable "db_backup_retention_days" {
  description = "Automated backups (make rds-snapshot takes manual snapshots before installs)."
  type        = number
  default     = 1
}

variable "db_final_snapshot_identifier" {
  description = "Final snapshot taken by terraform destroy (plan v1.0 step 10)."
  type        = string
  default     = "temporal-db-final"
}

# --- IRSA ------------------------------------------------------------------------------

variable "model_id" {
  description = "Foundation model behind the pinned inference profiles (ADR-0001)."
  type        = string
  default     = "anthropic.claude-opus-5-5"
}

variable "inference_profiles" {
  description = "Inference profiles Finance may call (global default, us. fallback)."
  type        = list(string)
  default     = ["global.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5-5"]
}

variable "invocation_role_name" {
  description = "The CFN-managed invocation role the WCI assumes (scripts/invoke-role.sh; runbook Phase 6)."
  type        = string
  default     = "Temporal-AgentCore-Worker"
}

variable "ecr_repositories" {
  description = "ECR repositories for the EKS images (v0.11 make finance-image)."
  type        = list(string)
  default     = ["agentcore-demo/finance-agent", "agentcore-demo/payments-mock"]
}
