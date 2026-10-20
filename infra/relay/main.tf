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
  region = "eu-south-2"
  default_tags {
    tags = {
      Project   = "swarm-relay"
      ManagedBy = "Terraform"
    }
  }
}

provider "aws" {
  alias  = "global"
  region = "us-east-1"
}

variable "agent_key" {
  description = "Existing Swarm agent key, supplied through a private ignored tfvars file."
  type        = string
  sensitive   = true
}

variable "allowed_https_cidrs" {
  description = "Campus and coordinator public source CIDRs. HTTPS is not globally open."
  type        = set(string)
  validation {
    condition     = length(var.allowed_https_cidrs) > 0 && alltrue([for cidr in var.allowed_https_cidrs : can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0"])
    error_message = "Supply explicit public IPv4 source CIDRs; do not use 0.0.0.0/0."
  }
}

variable "instance_type" {
  type    = string
  default = "t4g.micro"
  validation {
    condition     = contains(["t4g.micro", "t4g.small"], var.instance_type)
    error_message = "Use a small ARM CPU relay: t4g.micro or t4g.small."
  }
}

variable "max_compute_hourly_usd" {
  type    = number
  default = 0.02
  validation {
    condition     = var.max_compute_hourly_usd > 0 && var.max_compute_hourly_usd <= 0.02
    error_message = "The compute budget must be positive and no more than $0.02/hour."
  }
}

variable "max_runtime_hours" {
  description = "Guest timer terminates compute and its root disk after this many hours. Destroy removes remaining Terraform resources."
  type        = number
  default     = 4
  validation {
    condition     = var.max_runtime_hours >= 1 && var.max_runtime_hours <= 8 && floor(var.max_runtime_hours) == var.max_runtime_hours
    error_message = "Use an integer between 1 and 8 hours."
  }
}

variable "dns_name" {
  type    = string
  default = "swarm-relay.yerson.co"
  validation {
    condition     = can(regex("^[a-z0-9-]+\\.yerson\\.co$", var.dns_name))
    error_message = "Use one relay subdomain of the existing yerson.co zone."
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
}

data "aws_route53_zone" "existing" {
  provider     = aws.global
  name         = "yerson.co."
  private_zone = false
}

data "aws_pricing_product" "relay" {
  provider     = aws.global
  service_code = "AmazonEC2"
  filters {
    field = "regionCode"
    value = "eu-south-2"
  }
  filters {
    field = "instanceType"
    value = var.instance_type
  }
  filters {
    field = "operatingSystem"
    value = "Linux"
  }
  filters {
    field = "tenancy"
    value = "Shared"
  }
  filters {
    field = "preInstalledSw"
    value = "NA"
  }
  filters {
    field = "capacitystatus"
    value = "Used"
  }
}

locals {
  compute_hourly_usd = one(distinct(flatten([
    for term in values(jsondecode(data.aws_pricing_product.relay.result).terms.OnDemand) : [
      for rate in values(term.priceDimensions) : tonumber(rate.pricePerUnit.USD) if rate.unit == "Hrs"
    ]
  ])))
}

resource "random_password" "control" {
  length  = 40
  special = false
}

resource "aws_ssm_parameter" "keys" {
  name  = "/swarm-relay/keys"
  type  = "SecureString"
  value = jsonencode({ AGENT_KEY = var.agent_key, CONTROL_KEY = random_password.control.result })
}

resource "aws_iam_role" "relay" {
  name_prefix = "swarm-relay-"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.relay.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "keys" {
  role = aws_iam_role.relay.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.keys.arn }]
  })
}

resource "aws_iam_instance_profile" "relay" {
  name_prefix = "swarm-relay-"
  role        = aws_iam_role.relay.name
}

resource "aws_security_group" "relay" {
  name_prefix = "swarm-relay-"
  description = "Restricted HTTPS relay; public HTTP only for ACME certificate validation"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  for_each          = var.allowed_https_cidrs
  security_group_id = aws_security_group.relay.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_ingress_rule" "acme" {
  security_group_id = aws_security_group.relay.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "outbound" {
  security_group_id = aws_security_group.relay.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_instance" "relay" {
  ami                         = data.aws_ssm_parameter.ubuntu.value
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.relay.id]
  iam_instance_profile        = aws_iam_instance_profile.relay.name
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    hostname        = var.dns_name
    keys_parameter  = aws_ssm_parameter.keys.name
    runtime_seconds = var.max_runtime_hours * 3600
    relay_server    = base64gzip(file("${path.module}/../../coordinator/relay_server.py"))
    relay_transport = base64gzip(file("${path.module}/../../coordinator/relay_transport.py"))
    requirements    = filebase64("${path.module}/../../coordinator/requirements.txt")
  })
  instance_initiated_shutdown_behavior = "terminate"
  credit_specification {
    cpu_credits = "standard"
  }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_size           = 8
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }
  lifecycle {
    precondition {
      condition     = local.compute_hourly_usd <= var.max_compute_hourly_usd
      error_message = "The live EC2 rate exceeds the trial's compute budget."
    }
  }
  depends_on = [aws_iam_role_policy.keys, aws_iam_role_policy_attachment.ssm]
  tags       = { Name = "swarm-relay" }
}

resource "aws_route53_record" "relay" {
  provider = aws.global
  zone_id  = data.aws_route53_zone.existing.zone_id
  name     = var.dns_name
  type     = "A"
  ttl      = 60
  records  = [aws_instance.relay.public_ip]
}

output "deployment" {
  value = {
    region             = "eu-south-2"
    instance_id        = aws_instance.relay.id
    public_ip          = aws_instance.relay.public_ip
    url                = "https://${aws_route53_record.relay.fqdn}"
    compute_hourly_usd = local.compute_hourly_usd
    max_runtime_hours  = var.max_runtime_hours
    default_vpc_id     = data.aws_vpc.default.id
    subnet_id          = aws_instance.relay.subnet_id
  }
}

output "control_key" {
  value     = random_password.control.result
  sensitive = true
}
