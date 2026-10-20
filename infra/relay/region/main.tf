terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

# Region activation has no hourly charge. Keep it in its own state so the
# deployment provider is configured only after regional IAM propagation.
resource "aws_account_region" "spain" {
  region_name = "eu-south-2"
  enabled     = true
}
