# The two application IRSA roles (runbook 4.1 and 7b.3). Separate roles, never shared.

# temporal-server-wci: the Temporal worker service's WCI. It may only assume the invocation
# role (created later from Temporal's template by scripts/invoke-role.sh; ADR-0007).
data "aws_iam_policy_document" "temporal_server_wci" {
  statement {
    sid       = "AssumeInvocationRoleOnly"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:${local.partition}:iam::${local.account}:role/${var.invocation_role_name}"]
  }
}

module "temporal_server_wci" {
  source = "../modules/irsa-role"

  name              = "temporal-server-wci"
  description       = "Temporal worker service (WCI) on EKS: may only assume the AgentCore invocation role (agentcore-self-hosted demo)"
  oidc_provider_arn = module.eks.oidc_provider_arn
  namespace         = "temporal"
  service_account   = "temporal-worker"
  policy_json       = data.aws_iam_policy_document.temporal_server_wci.json
}

# agent-services-worker: Finance's Nexus handler Worker; Bedrock on the pinned Opus 5.5
# profiles only (same resource set as the AgentCore execution role, 30-agentcore-iam).
data "aws_iam_policy_document" "agent_services_worker" {
  statement {
    sid     = "BedrockPinnedModel"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(
      [for p in var.inference_profiles : "arn:${local.partition}:bedrock:${var.region}:${local.account}:inference-profile/${p}"],
      [
        # Regional copies behind the us./global. profiles, and the global (Region-less) ARN.
        "arn:${local.partition}:bedrock:*::foundation-model/${var.model_id}",
        "arn:${local.partition}:bedrock:::foundation-model/${var.model_id}",
      ],
    )
  }
}

module "agent_services_worker" {
  source = "../modules/irsa-role"

  name              = "agent-services-worker"
  description       = "Finance Nexus handler Worker on EKS: Bedrock Opus 5.5 profiles only (agentcore-self-hosted demo)"
  oidc_provider_arn = module.eks.oidc_provider_arn
  namespace         = "temporal-workers"
  service_account   = "agent-services-worker"
  policy_json       = data.aws_iam_policy_document.agent_services_worker.json
}
