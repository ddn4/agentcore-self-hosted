module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.29" # verified 21.29.0 (registry, 2026-10-31); v21 names: name, kubernetes_version, endpoint_*, addons

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id     = local.net.vpc_id
  subnet_ids = local.net.private_subnet_ids

  endpoint_private_access      = true
  endpoint_public_access       = true # demo: Terraform/Helm/kubectl from the laptop
  endpoint_public_access_cidrs = var.public_access_cidrs

  enable_irsa                              = true # IAM OIDC provider (runbook 4.1, 7b.3)
  enable_cluster_creator_admin_permissions = true

  # EKS already envelope-encrypts Kubernetes API data with an AWS-owned key; a customer KMS
  # key would add $1/month and a 7-30 day deletion wait on teardown.
  encryption_config = null
  create_kms_key    = false

  enabled_log_types                      = ["api", "audit", "authenticator"]
  cloudwatch_log_group_retention_in_days = var.control_plane_log_retention_days

  addons = {
    vpc-cni = {
      before_compute = true
      # The network-policy agent (runbook 7b.6; ledger 37): NetworkPolicy enforcement in the
      # aws-node DaemonSet, as kindnet did on kind (v0.9).
      configuration_values = jsonencode({ enableNetworkPolicy = "true" })
    }
    kube-proxy = {}
    coredns    = {}
  }

  eks_managed_node_groups = {
    default = {
      ami_type       = var.node_ami_type
      instance_types = [var.node_instance_type]
      min_size       = var.node_min_size
      max_size       = var.node_max_size
      desired_size   = var.node_desired_size
      labels         = { "agentcore-demo/pool" = "default" }
    }
  }

  # Launch template tag specifications do not inherit provider default_tags.
  tags = local.tags
}

# The node SG reaches RDS and the interface endpoints (sg-rds and sg-vpce are 10-network's).
resource "aws_vpc_security_group_ingress_rule" "rds_from_nodes" {
  security_group_id            = local.sg.rds
  description                  = "EKS nodes and pods to PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = module.eks.node_security_group_id
}

resource "aws_vpc_security_group_ingress_rule" "vpce_from_nodes" {
  security_group_id            = local.sg.vpce
  description                  = "EKS nodes and pods to interface endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = module.eks.node_security_group_id
}

# v0.11: pods in the VPC reach the frontend through the internal NLB by its Route 53 name (the
# in-VPC mTLS checks of plan v0.11; the same path an AgentCore ENI takes). sg-nlb-temporal-frontend
# otherwise admits only sg-agentcore-worker (10-network). The NLB -> pod rules are the LBC's
# (manage-backend-security-group-rules), not Terraform's.
resource "aws_vpc_security_group_ingress_rule" "nlb_from_nodes" {
  security_group_id            = local.sg.nlb_temporal_frontend
  description                  = "EKS nodes and pods to the Temporal frontend NLB (in-VPC checks)"
  ip_protocol                  = "tcp"
  from_port                    = 7233
  to_port                      = 7233
  referenced_security_group_id = module.eks.node_security_group_id
}
