# ARNs and endpoints carry the account ID or resource IDs: read them with
# `terraform output` (make render-config), never paste them into committed files.
output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_version" {
  value = module.eks.cluster_version
}

output "oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

output "node_security_group_id" {
  value = module.eks.node_security_group_id
}

output "node_group_name" {
  description = "EKS managed node group name (make pause / resume)."
  value       = split(":", module.eks.eks_managed_node_groups["default"].node_group_id)[1]
}

output "rds_identifier" {
  value = aws_db_instance.temporal.identifier
}

output "rds_address" {
  value = aws_db_instance.temporal.address
}

output "rds_port" {
  value = aws_db_instance.temporal.port
}

output "rds_engine_version" {
  value = aws_db_instance.temporal.engine_version_actual
}

output "rds_master_secret_arn" {
  description = "Secrets Manager secret RDS manages for the master user (no password in state)."
  value       = aws_db_instance.temporal.master_user_secret[0].secret_arn
}

output "temporal_server_wci_role_arn" {
  value = module.temporal_server_wci.arn
}

output "agent_services_worker_role_arn" {
  value = module.agent_services_worker.arn
}

output "irsa_subjects" {
  value = {
    temporal_server_wci   = module.temporal_server_wci.subject
    agent_services_worker = module.agent_services_worker.subject
  }
}

output "lbc_role_arn" {
  value = module.lbc_irsa.arn
}

output "ecr_repository_urls" {
  value = { for k, v in aws_ecr_repository.this : k => v.repository_url }
}
