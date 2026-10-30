variable "region" {
  description = "The only Region this project uses."
  type        = string
  default     = "us-east-1"
}

variable "execution_role_name" {
  description = "AgentCore Runtime execution role for the Support Worker."
  type        = string
  default     = "agentcore-demo-support-exec"
}

variable "runtime_name_prefix" {
  description = "AgentCore runtime names this role serves (CLI: <project>_<runtime>). Scopes log-group and workload-identity statements."
  type        = string
  default     = "AgentcoreDemo_"
}

variable "model_id" {
  description = "Foundation model behind the pinned inference profiles (ADR-0001)."
  type        = string
  default     = "anthropic.claude-opus-5-5"
}

variable "inference_profiles" {
  description = "Inference profiles the Worker may call (global default, us. fallback)."
  type        = list(string)
  default     = ["global.anthropic.claude-opus-5-5", "us.anthropic.claude-opus-5-5"]
}

variable "mtls_secret_prefixes" {
  description = "Secrets Manager names (without the random suffix) the Worker may read at startup (runbook 4.2, 5.3)."
  type        = list(string)
  default     = ["temporal/agentcore-worker-mtls"]
}

variable "create_dev_wci_role" {
  description = "v0.8: create temporal-dev-wci, the laptop dev server's stand-in for temporal-server-wci (IRSA on EKS)."
  type        = bool
  default     = false
}

variable "operator_role_name_pattern" {
  description = "IAM role name pattern of the humans allowed to assume temporal-dev-wci (the SSO admin role)."
  type        = string
  default     = "AWSReservedSSO_AWSAdministratorAccess_*"
}

variable "invocation_role_name" {
  description = "The CFN-managed invocation role (runbook Phase 6 template default; scripts/invoke-role.sh)."
  type        = string
  default     = "Temporal-AgentCore-Worker"
}
