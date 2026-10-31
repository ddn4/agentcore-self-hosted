# AWS foundation, network half (plan v0.10; runbook Phase 1.1, 1.4, 1.5).
# A greenfield VPC whose private subnets are placed by AZ ID (AgentCore VPC mode accepts
# only use1-az1/az2/az4), one NAT gateway (one EIP: the account is at 8/10), flow logs,
# the interface endpoints the WCI, the Worker code and the AgentCore ENIs need, an S3
# gateway endpoint, four security groups and the Route 53 private zone.

data "aws_availability_zones" "agentcore" {
  state = "available"
  filter {
    name   = "zone-id"
    values = var.agentcore_az_ids
  }
}

locals {
  # Fail early if the Region stops offering one of the AZ IDs.
  az_ids_ok = length(data.aws_availability_zones.agentcore.zone_ids) == length(var.agentcore_az_ids)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7" # verified 6.7.3 (registry, 2026-10-31); v6 accepts AZ IDs in `azs`

  name = var.name
  cidr = var.cidr
  # AZ IDs, not names: the module sets availability_zone_id on every subnet.
  azs             = var.agentcore_az_ids
  private_subnets = var.private_subnets
  public_subnets  = var.public_subnets

  enable_nat_gateway = true
  single_nat_gateway = true # one EIP; one NAT per AZ for production

  enable_dns_support   = true
  enable_dns_hostnames = true

  enable_flow_log                                 = true
  create_flow_log_cloudwatch_log_group            = true
  create_flow_log_cloudwatch_iam_role             = true
  flow_log_cloudwatch_log_group_retention_in_days = var.flow_log_retention_days
  flow_log_max_aggregation_interval               = 60
  flow_log_traffic_type                           = "ALL"

  # LBC subnet discovery: internal NLB (v0.11) in the private subnets.
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = "1" }
  public_subnet_tags  = { "kubernetes.io/role/elb" = "1" }
}

check "agentcore_az_ids" {
  assert {
    condition     = local.az_ids_ok
    error_message = "us-east-1 no longer offers every AgentCore AZ ID in var.agentcore_az_ids."
  }
}
