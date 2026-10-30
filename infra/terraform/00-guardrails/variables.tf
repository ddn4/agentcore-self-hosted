variable "region" {
  description = "The only Region this project uses."
  type        = string
  default     = "us-east-1"
}

variable "budget_email" {
  description = "Subscriber address for the AWS Budgets notifications. Set TF_VAR_budget_email (never commit it)."
  type        = string
  sensitive   = true
}

variable "budget_limit_usd" {
  description = "Monthly cost budget for the account in USD (plan section 8)."
  type        = number
  default     = 200
}

variable "active_sessions_threshold" {
  description = "Alarm when the account's AgentCore Runtime ActiveSessionCount stays above this (the account has other runtimes: baseline <= 5)."
  type        = number
  default     = 10
}
