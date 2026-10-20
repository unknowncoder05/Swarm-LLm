terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.name
      ManagedBy = "Terraform"
      Purpose   = "Disposable distributed inference trial"
    }
  }
}

provider "aws" {
  alias  = "pricing"
  region = "us-east-1"
}
