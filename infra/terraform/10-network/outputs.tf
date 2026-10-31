# Resource IDs: read with `terraform output`, never paste them into committed files
# (scripts pipe them through scripts/redact.sh).
output "vpc_id" {
  value = module.vpc.vpc_id
}

output "vpc_cidr_block" {
  value = module.vpc.vpc_cidr_block
}

output "private_subnet_ids" {
  value = module.vpc.private_subnets
}

output "private_subnet_az_ids" {
  description = "AZ ID per private subnet, in the same order (AgentCore VPC mode)."
  value       = var.agentcore_az_ids
}

output "public_subnet_ids" {
  value = module.vpc.public_subnets
}

output "private_route_table_ids" {
  value = module.vpc.private_route_table_ids
}

output "security_group_ids" {
  value = {
    agentcore_worker      = aws_security_group.agentcore_worker.id
    nlb_temporal_frontend = aws_security_group.nlb_frontend.id
    vpce                  = aws_security_group.vpce.id
    rds                   = aws_security_group.rds.id
  }
}

output "interface_endpoint_ids" {
  value = { for k, v in aws_vpc_endpoint.interface : k => v.id }
}

output "s3_gateway_endpoint_id" {
  value = aws_vpc_endpoint.s3.id
}

output "s3_prefix_list_id" {
  value = aws_vpc_endpoint.s3.prefix_list_id
}

output "private_zone_id" {
  value = aws_route53_zone.internal.zone_id
}

output "private_zone_name" {
  value = aws_route53_zone.internal.name
}

output "flow_log_destination_arn" {
  value = module.vpc.vpc_flow_log_destination_arn
}
