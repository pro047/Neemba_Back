# One-time adoption of resources hand-created with aws cli on 2026-07-15.
# `terraform apply` reads these, pulls the real resources into state, and — as
# long as the config matches reality — changes nothing in AWS. Safe to delete
# this file once the import apply has run (blocks are ignored after import,
# but deleting keeps the config honest).

import {
  to = aws_s3_bucket.db_backups
  id = "neemba-db-backups-989785488374"
}

import {
  to = aws_s3_bucket_public_access_block.db_backups
  id = "neemba-db-backups-989785488374"
}

import {
  to = aws_s3_bucket_lifecycle_configuration.db_backups
  id = "neemba-db-backups-989785488374"
}

import {
  to = aws_iam_role.neemba_ec2
  id = "neemba-ec2-role"
}

import {
  to = aws_iam_role_policy.db_backup_s3
  id = "neemba-ec2-role:neemba-db-backup-s3"
}

import {
  to = aws_iam_instance_profile.neemba_ec2
  id = "neemba-ec2-profile"
}
