# One IRSA role for exactly one ServiceAccount (runbook 4.1 trust):
#   Federated = the cluster's OIDC provider, sts:AssumeRoleWithWebIdentity,
#   <issuer>:aud = sts.amazonaws.com, <issuer>:sub = system:serviceaccount:<ns>:<sa>
# StringEquals (not StringLike): no wildcard ServiceAccounts.

locals {
  issuer = replace(var.oidc_provider_arn, "/^(.*provider/)/", "")
}

data "aws_iam_policy_document" "trust" {
  statement {
    sid     = "IrsaOneServiceAccount"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:sub"
      values   = ["system:serviceaccount:${var.namespace}:${var.service_account}"]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = var.name
  description          = var.description
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = var.max_session_duration
}

resource "aws_iam_role_policy" "this" {
  name   = var.name
  role   = aws_iam_role.this.id
  policy = var.policy_json
}
