# The neemba EC2 host's security group, adopted from the hand-built original
# (imports.tf). Rules are declared as standalone rule resources on purpose:
# an inline `ingress` block would make every apply delete rules terraform
# doesn't know about, and two kinds of rules here are dynamic by design —
#   * the gha-runner rule deploy.yml opens/revokes on port 22 mid-deploy
#   * the operator's pinned SSH /32 on port 22, updated from the console
#     whenever their IP changes (handover §5)
# Both stay OUT of terraform; standalone rule resources leave them alone.
#
# NOT managed here: the EC2 instance itself (see backup.tf header).

resource "aws_security_group" "neemba" {
  name        = "neemba"
  description = "neemba created 2025-09-15T07:04:08.014Z"
  vpc_id      = "vpc-0700780e525cd07c5"

  tags = {
    Name = "neemba"
  }
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.neemba.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.neemba.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "rtmp" {
  security_group_id = aws_security_group.neemba.id
  ip_protocol       = "tcp"
  from_port         = 1935
  to_port           = 1935
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.neemba.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}
