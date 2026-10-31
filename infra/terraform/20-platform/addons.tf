# AWS Load Balancer Controller (internal NLB with ip targets at v0.11) and cert-manager (the
# in-cluster CA, runbook 2.6), both by helm_release.

module "lbc_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = "~> 6.8" # verified 6.8.2 (registry, 2026-10-31)

  name            = "temporal-aws-load-balancer-controller"
  use_name_prefix = false
  policy_name     = "temporal-aws-load-balancer-controller"

  attach_load_balancer_controller_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }
}

resource "helm_release" "lbc" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = var.lbc_chart_version
  namespace  = "kube-system"
  wait       = true
  timeout    = 600

  values = [yamlencode({
    clusterName  = module.eks.cluster_name
    region       = var.region
    vpcId        = local.net.vpc_id
    replicaCount = 2
    serviceAccount = {
      create      = true
      name        = "aws-load-balancer-controller"
      annotations = { "eks.amazonaws.com/role-arn" = module.lbc_irsa.arn }
    }
  })]

  depends_on = [module.eks]
}

resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.cert_manager_chart_version
  namespace        = "cert-manager"
  create_namespace = true
  wait             = true
  timeout          = 600

  values = [yamlencode({
    crds = { enabled = true }
  })]

  # After the LBC: its Service webhook must be serving before other charts create Services.
  depends_on = [helm_release.lbc]
}
