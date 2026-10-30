terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
  }

  # Local state per stack (gitignored). Nothing secret goes into it: the
  # account ID comes from data.aws_caller_identity; no External ID here (ADR-0007).
  backend "local" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "agentcore-self-hosted"
      Stack     = "30-agentcore-iam"
      ManagedBy = "terraform"
    }
  }
}
