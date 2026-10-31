terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.70"
    }
  }

  # Local state per stack (gitignored). Nothing secret goes into it; 20-platform reads its
  # outputs through terraform_remote_state (backend "local", ../10-network/terraform.tfstate).
  backend "local" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "agentcore-self-hosted"
      Stack     = "10-network"
      ManagedBy = "terraform"
    }
  }
}
