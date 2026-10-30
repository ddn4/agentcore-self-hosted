# AgentCore IAM (plan v0.8 step 2; runbook 4.2). Two roles, both free:
#  - the Runtime execution role (what the Support Worker code runs as inside AgentCore)
#  - optionally temporal-dev-wci: the identity the laptop dev server's WCI runs as, allowed
#    only to assume the invocation role. It mirrors temporal-server-wci (IRSA) on EKS.
# NOT here (ADR-0007): the invocation role itself (CFN stack via scripts/invoke-role.sh)
# and its External ID (Secrets Manager via scripts/external-id.sh).

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  region    = var.region
}

# --- Runtime execution role ---------------------------------------------------------

data "aws_iam_policy_document" "exec_trust" {
  statement {
    sid     = "AssumeRolePolicy"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account}:*"]
    }
  }
}

# Base statements from the AWS "AgentCore Runtime execution role" (devguide
# runtime-permissions), without ECR (CodeZip, no container), Bedrock narrowed to the
# pinned model, plus the mTLS secret read (runbook 4.2).
data "aws_iam_policy_document" "exec" {
  statement {
    sid       = "LogGroups"
    actions   = ["logs:DescribeLogStreams", "logs:CreateLogGroup"]
    resources = ["arn:${local.partition}:logs:${local.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }
  statement {
    sid       = "LogGroupResourcePolicy"
    actions   = ["logs:PutResourcePolicy"]
    resources = ["arn:${local.partition}:logs:${local.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/${var.runtime_name_prefix}*"]
  }
  statement {
    sid       = "DescribeLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:${local.partition}:logs:${local.region}:${local.account}:log-group:*"]
  }
  statement {
    sid       = "LogEvents"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:${local.partition}:logs:${local.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
  }
  statement {
    sid       = "XRay"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
    resources = ["*"]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }
  statement {
    sid     = "WorkloadAccessToken"
    actions = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"]
    resources = [
      "arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account}:workload-identity-directory/default",
      "arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account}:workload-identity-directory/default/workload-identity/${var.runtime_name_prefix}*",
    ]
  }
  statement {
    sid     = "BedrockPinnedModel"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(
      [for p in var.inference_profiles : "arn:${local.partition}:bedrock:${local.region}:${local.account}:inference-profile/${p}"],
      [
        # Regional copies behind the us./global. profiles, and the global (Region-less) ARN.
        "arn:${local.partition}:bedrock:*::foundation-model/${var.model_id}",
        "arn:${local.partition}:bedrock:::foundation-model/${var.model_id}",
      ],
    )
  }
  statement {
    sid       = "MtlsSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for s in var.mtls_secret_prefixes : "arn:${local.partition}:secretsmanager:${local.region}:${local.account}:secret:${s}-*"]
  }
}

resource "aws_iam_role" "exec" {
  name                 = var.execution_role_name
  description          = "AgentCore Runtime execution role: Support Worker (agentcore-self-hosted demo)"
  assume_role_policy   = data.aws_iam_policy_document.exec_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "exec" {
  name   = "agentcore-demo-support-exec"
  role   = aws_iam_role.exec.id
  policy = data.aws_iam_policy_document.exec.json
}

# --- temporal-dev-wci (v0.8 only) ---------------------------------------------------

data "aws_iam_policy_document" "dev_wci_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account}:root"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values   = ["arn:${local.partition}:iam::${local.account}:role/aws-reserved/sso.amazonaws.com/*/${var.operator_role_name_pattern}"]
    }
  }
}

data "aws_iam_policy_document" "dev_wci" {
  statement {
    sid       = "AssumeInvocationRoleOnly"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:${local.partition}:iam::${local.account}:role/${var.invocation_role_name}"]
  }
}

resource "aws_iam_role" "dev_wci" {
  count                = var.create_dev_wci_role ? 1 : 0
  name                 = "temporal-dev-wci"
  description          = "Laptop dev server WCI identity: may only assume the AgentCore invocation role (agentcore-self-hosted demo)"
  assume_role_policy   = data.aws_iam_policy_document.dev_wci_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "dev_wci" {
  count  = var.create_dev_wci_role ? 1 : 0
  name   = "assume-invocation-role"
  role   = aws_iam_role.dev_wci[0].id
  policy = data.aws_iam_policy_document.dev_wci.json
}
