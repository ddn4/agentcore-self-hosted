terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3"
    }
  }

  # Local state per stack (gitignored). No passwords in it: RDS keeps its master password in
  # a Secrets Manager secret it manages itself (manage_master_user_password); the root CA is
  # cert-manager's (ADR-0007). The laptop /32 comes from the gitignored terraform.auto.tfvars.
  backend "local" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}

# Helm talks to the cluster's public endpoint (limited to the laptop /32) with a token from
# the operator's own AWS credentials (`aws eks get-token`); nothing is written to kubeconfig.
provider "helm" {
  kubernetes = {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
    }
  }
}
