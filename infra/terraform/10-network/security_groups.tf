# Security groups (runbook 1.5). AWS reserves the "sg-" prefix for IDs, so a group NAME
# cannot start with it: the runbook's sg-<role> names are the Role tag; the names are
# agentcore-demo-<role>. Rules that need the EKS node SG live in 20-platform.

resource "aws_security_group" "agentcore_worker" {
  name        = "agentcore-demo-agentcore-worker"
  description = "AgentCore Runtime ENIs (Support Worker): outbound only"
  vpc_id      = module.vpc.vpc_id
  tags        = { Name = "agentcore-demo-agentcore-worker", Role = "sg-agentcore-worker" }
}

resource "aws_security_group" "nlb_frontend" {
  name        = "agentcore-demo-nlb-temporal-frontend"
  description = "Internal NLB in front of the Temporal frontend (TLS passthrough, 7233)"
  vpc_id      = module.vpc.vpc_id
  tags        = { Name = "agentcore-demo-nlb-temporal-frontend", Role = "sg-nlb-temporal-frontend" }
}

resource "aws_security_group" "vpce" {
  name        = "agentcore-demo-vpce"
  description = "Interface VPC endpoints: 443 from AgentCore ENIs and EKS nodes"
  vpc_id      = module.vpc.vpc_id
  tags        = { Name = "agentcore-demo-vpce", Role = "sg-vpce" }
}

resource "aws_security_group" "rds" {
  name        = "agentcore-demo-rds"
  description = "RDS PostgreSQL for Temporal: 5432 from EKS nodes"
  vpc_id      = module.vpc.vpc_id
  tags        = { Name = "agentcore-demo-rds", Role = "sg-rds" }
}

# --- sg-agentcore-worker: no ingress; egress to the NLB, the endpoints and S3 ----------

resource "aws_vpc_security_group_egress_rule" "agentcore_to_nlb" {
  security_group_id            = aws_security_group.agentcore_worker.id
  description                  = "Temporal frontend via the internal NLB"
  ip_protocol                  = "tcp"
  from_port                    = var.frontend_port
  to_port                      = var.frontend_port
  referenced_security_group_id = aws_security_group.nlb_frontend.id
}

resource "aws_vpc_security_group_egress_rule" "agentcore_to_vpce" {
  security_group_id            = aws_security_group.agentcore_worker.id
  description                  = "Interface endpoints (bedrock-runtime, sts, secretsmanager, logs, agentcore)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.vpce.id
}

resource "aws_vpc_security_group_egress_rule" "agentcore_to_s3" {
  security_group_id = aws_security_group.agentcore_worker.id
  description       = "S3 gateway endpoint (managed prefix list; runbook 1.5, AWS Q2)"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
}

# --- sg-nlb-temporal-frontend ----------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "nlb_from_agentcore" {
  security_group_id            = aws_security_group.nlb_frontend.id
  description                  = "AgentCore Worker to frontend"
  ip_protocol                  = "tcp"
  from_port                    = var.frontend_port
  to_port                      = var.frontend_port
  referenced_security_group_id = aws_security_group.agentcore_worker.id
}

resource "aws_vpc_security_group_egress_rule" "nlb_to_targets" {
  security_group_id = aws_security_group.nlb_frontend.id
  description       = "NLB to frontend pod IPs (ip targets) in the VPC"
  ip_protocol       = "tcp"
  from_port         = var.frontend_port
  to_port           = var.frontend_port
  cidr_ipv4         = module.vpc.vpc_cidr_block
}

# --- sg-vpce ---------------------------------------------------------------------------

resource "aws_vpc_security_group_ingress_rule" "vpce_from_agentcore" {
  security_group_id            = aws_security_group.vpce.id
  description                  = "AgentCore Worker to endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.agentcore_worker.id
}
