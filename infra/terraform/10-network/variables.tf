variable "region" {
  description = "The only Region this project uses."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "VPC name (runbook 1.1)."
  type        = string
  default     = "temporal-customer-support"
}

variable "cidr" {
  description = "VPC CIDR (runbook 1.1)."
  type        = string
  default     = "10.40.0.0/16"
}

variable "agentcore_az_ids" {
  description = "AgentCore VPC mode supports only these AZ IDs in us-east-1 (runbook 3.2). Subnets are placed by AZ ID, never by AZ name."
  type        = list(string)
  default     = ["use1-az1", "use1-az2", "use1-az4"]
}

variable "private_subnets" {
  description = "One private subnet per AgentCore AZ ID (EKS nodes and pods, RDS, AgentCore ENIs, endpoints)."
  type        = list(string)
  default     = ["10.40.0.0/19", "10.40.32.0/19", "10.40.64.0/19"]
}

variable "public_subnets" {
  description = "Public subnets: the single NAT gateway (and internet-facing load balancers, unused)."
  type        = list(string)
  default     = ["10.40.192.0/24", "10.40.193.0/24", "10.40.194.0/24"]
}

variable "interface_endpoints" {
  description = "Interface endpoints (runbook 1.4): WCI (bedrock-agentcore, -control, sts), Worker code (secretsmanager, sts, bedrock-runtime), AgentCore ENIs (logs). ecr.* only for Container builds (we use CodeZip)."
  type        = list(string)
  default     = ["bedrock-agentcore", "bedrock-agentcore-control", "sts", "secretsmanager", "bedrock-runtime", "logs"]
}

variable "endpoint_subnet_count" {
  description = "How many private subnets (AZs) carry an ENI of each interface endpoint. 2 halves nothing functionally (private DNS answers from any AZ) and saves 6 x $0.01/h (ADR-0006)."
  type        = number
  default     = 2
}

variable "flow_log_retention_days" {
  description = "Retention of the VPC flow log group (v0.12 reads the AgentCore ENI -> NLB:7233 flows)."
  type        = number
  default     = 7
}

variable "private_zone_name" {
  description = "Route 53 private hosted zone (runbook 3.4 decided name)."
  type        = string
  default     = "temporal-customer-support.internal"
}

variable "frontend_port" {
  description = "Temporal frontend gRPC port (NLB listener, runbook 1.5)."
  type        = number
  default     = 7233
}
