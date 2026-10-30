terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
  }

  # Local state per stack (gitignored). Nothing secret goes into it: the budget's
  # subscriber address comes from TF_VAR_budget_email and is the only personal value.
  backend "local" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "agentcore-self-hosted"
      Stack     = "00-guardrails"
      ManagedBy = "terraform"
    }
  }
}
