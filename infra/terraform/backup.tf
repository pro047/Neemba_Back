# Daily pg_dump backup storage + the credentials path for the pg-backup
# sidecar (docker-compose.prod.yml). Created 2026-07-15 via aws cli, then
# adopted into terraform with the import blocks in imports.tf.
#
# NOT managed here: the EC2 instance itself and its instance-profile
# association (aws ec2 associate-iam-instance-profile) — the provider has no
# standalone association resource and importing a hand-built prod instance
# risks replacement-shaped plans. See README.md for the manual command.

resource "aws_s3_bucket" "db_backups" {
  bucket = "neemba-db-backups-989785488374"
}

resource "aws_s3_bucket_public_access_block" "db_backups" {
  bucket = aws_s3_bucket.db_backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "db_backups" {
  bucket = aws_s3_bucket.db_backups.id

  transition_default_minimum_object_size = "all_storage_classes_128K"

  rule {
    id     = "expire-pg-dumps-90d"
    status = "Enabled"

    filter {
      prefix = "pg/"
    }

    expiration {
      days = 90
    }
  }
}

# Instance role for the neemba EC2 host. Containers reach the temporary
# credentials via IMDS (hop limit 2). Scope stays at "what the host may do";
# today that is only shipping backups to the pg/ prefix.
resource "aws_iam_role" "neemba_ec2" {
  name = "neemba-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "db_backup_s3" {
  name = "neemba-db-backup-s3"
  role = aws_iam_role.neemba_ec2.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.db_backups.arn}/pg/*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.db_backups.arn
        Condition = {
          StringLike = { "s3:prefix" = "pg/*" }
        }
      }
    ]
  })
}

resource "aws_iam_instance_profile" "neemba_ec2" {
  name = "neemba-ec2-profile"
  role = aws_iam_role.neemba_ec2.name
}
