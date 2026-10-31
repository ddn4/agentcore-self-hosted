# VPC endpoints (runbook 1.4). NAT exists too, so these keep AWS API traffic private rather
# than being the only path; v0.12 records which ones AgentCore VPC mode really needs (AWS Q2).

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(var.interface_endpoints)

  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = slice(module.vpc.private_subnets, 0, var.endpoint_subnet_count)
  security_group_ids  = [aws_security_group.vpce.id]

  tags = { Name = "agentcore-demo-${each.value}" }
}

# S3 gateway endpoint on the private route tables. AgentCore's CodeZip pulls come from the
# service-owned acr-code-* buckets (runbook 1.4). The policy is left at full access on
# purpose: a gateway endpoint applies to ALL S3 traffic from these subnets, and image pulls
# that land on S3 in us-east-1 (ECR layers in prod-us-east-1-starport-layer-bucket, quay.io
# blobs for cert-manager) would break under a policy that allows only acr-code-*.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.private_route_table_ids

  tags = { Name = "agentcore-demo-s3" }
}
