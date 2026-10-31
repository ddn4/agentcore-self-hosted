# AWS foundation, platform half (plan v0.10; runbook Phase 1.2, 1.3, 4.1, 7b.3).
# EKS (IRSA, public endpoint for the laptop /32 only, VPC CNI network-policy agent, managed
# arm64 nodes), the AWS Load Balancer Controller and cert-manager, RDS PostgreSQL 16, two
# narrow IRSA roles and the ECR repositories. Network IDs come from 10-network's state.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "terraform_remote_state" "network" {
  backend = "local"
  config = {
    path = var.network_state_path
  }
}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  net       = data.terraform_remote_state.network.outputs
  sg        = local.net.security_group_ids

  tags = {
    Project   = "agentcore-self-hosted"
    Stack     = "20-platform"
    ManagedBy = "terraform"
  }
}
