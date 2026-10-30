# ARNs carry the account ID: read them with `terraform output -raw`, never paste them
# into committed files (scripts pipe them through scripts/redact.sh).
output "execution_role_arn" {
  value = aws_iam_role.exec.arn
}

output "dev_wci_role_arn" {
  value = var.create_dev_wci_role ? aws_iam_role.dev_wci[0].arn : ""
}

output "invocation_role_name" {
  value = var.invocation_role_name
}
