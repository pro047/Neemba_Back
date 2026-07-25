# GitHub Actions OIDC roles — replaces the static admin key in GHA secrets.
# Per-purpose role split: the write-capable deploy role trusts main only;
# the read-only watch role also trusts develop so a workflow_dispatch run can
# verify the OIDC plumbing before a release. Do NOT widen the deploy trust.
#
# The OIDC provider itself is account-shared (created for hymn) — referenced
# as data, never imported here: absorbing it into this state would let a
# neemba destroy take hymn's CI down with it.

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# --- deploy: opens/closes SSH ingress for the runner (deploy.yml) ---

resource "aws_iam_role" "gha_deploy" {
  name = "neemba-gha-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:pro047/Neemba_Back:ref:refs/heads/main"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "gha_deploy_sg" {
  name = "neemba-gha-deploy-sg"
  role = aws_iam_role.gha_deploy.id

  # Exactly what scripts/update-gha-ssh-ips.sh calls, scoped to the one SG.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress",
      ]
      Resource = aws_security_group.neemba.arn
    }]
  })
}

# --- watch: reads CPU credit metrics (cpu-credit-watch.yml) ---

resource "aws_iam_role" "gha_watch" {
  name = "neemba-gha-watch"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = [
            "repo:pro047/Neemba_Back:ref:refs/heads/main",
            "repo:pro047/Neemba_Back:ref:refs/heads/develop",
          ]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "gha_watch_cloudwatch" {
  name = "neemba-gha-watch-cloudwatch"
  role = aws_iam_role.gha_watch.id

  # GetMetricStatistics does not support resource-level scoping — "*" is the
  # narrowest possible; the action itself is read-only.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "cloudwatch:GetMetricStatistics"
      Resource = "*"
    }]
  })
}
