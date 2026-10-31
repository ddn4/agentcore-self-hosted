# RDS PostgreSQL 16 for Temporal persistence and SQL visibility (runbook 1.3; ADR-0006 sizing).

resource "aws_db_subnet_group" "temporal" {
  name        = var.db_identifier
  description = "Temporal persistence (private subnets on the AgentCore AZ IDs)"
  subnet_ids  = local.net.private_subnet_ids
}

# rds.force_ssl is already 1 in the postgres16 default group (verify item 1.3); it is pinned
# here so the TLS requirement is visible in code and survives a default change.
resource "aws_db_parameter_group" "temporal" {
  name        = "${var.db_identifier}-pg16"
  family      = "postgres16"
  description = "Temporal PostgreSQL 16: TLS required"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
    # RDS reports a value equal to the family default as pending-reboot (a declared
    # "immediate" shows as drift on every plan); nothing is pending, 1 is already in effect.
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "temporal" {
  identifier     = var.db_identifier
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"
  storage_encrypted = true

  username = "postgres"
  # RDS creates and rotates the password in its own Secrets Manager secret: no literal, and
  # nothing secret in Terraform state (plan section 3.2).
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.temporal.name
  parameter_group_name   = aws_db_parameter_group.temporal.name
  vpc_security_group_ids = [local.sg.rds]
  publicly_accessible    = false
  multi_az               = false
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  backup_retention_period    = var.db_backup_retention_days
  copy_tags_to_snapshot      = true
  auto_minor_version_upgrade = false # the minor is recorded in docs/versions.md
  apply_immediately          = true

  deletion_protection       = false
  skip_final_snapshot       = false
  final_snapshot_identifier = var.db_final_snapshot_identifier
}
