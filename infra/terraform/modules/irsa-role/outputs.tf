output "arn" {
  description = "Role ARN (carries the account ID: never commit it)."
  value       = aws_iam_role.this.arn
}

output "name" {
  value = aws_iam_role.this.name
}

output "subject" {
  description = "The one ServiceAccount the trust policy accepts."
  value       = "system:serviceaccount:${var.namespace}:${var.service_account}"
}
