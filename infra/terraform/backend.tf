# Remote state in the account's shared tfstate bucket (versioning enabled),
# one key per project. No state locking: terraform 1.9 needs DynamoDB for S3
# locking and this is a single-operator account — revisit if a second operator
# or CI starts running terraform.
terraform {
  required_version = ">= 1.9"

  backend "s3" {
    bucket = "hymn-tfstate"
    key    = "neemba/terraform.tfstate"
    region = "ap-northeast-2"
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "ap-northeast-2"
}
