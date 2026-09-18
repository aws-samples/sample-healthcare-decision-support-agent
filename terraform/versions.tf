# ============================================================================
# Medical Nudging AgentCore - Terraform and Provider Versions
# ============================================================================

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # >= 6.47 adds aws_bedrockagentcore_online_evaluation_config (terraform/evaluation.tf).
      version = ">= 6.47, < 7.0"
    }
    # hashicorp/aws has no HealthLake resources, so the datastore is created
    # through the Cloud Control API provider. See terraform/healthlake.tf.
    awscc = {
      source  = "hashicorp/awscc"
      version = "~> 1.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}

provider "awscc" {
  region  = var.aws_region
  profile = var.aws_profile
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project                    = "MedicalNudging"
      Pattern                    = "agentcore-runtime"
      Environment                = var.environment
      StackName                  = var.stack_name
      ManagedBy                  = "Terraform"
      "aws-control-tower:backup" = "true"
    }
  }
}
