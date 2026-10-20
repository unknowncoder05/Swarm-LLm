data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_servicequotas_service_quota" "gpu" {
  service_code = "ec2"
  quota_code   = local.small ? "L-DB2E81BA" : "L-3819A6DF"
}

data "aws_ec2_instance_type" "gpu" {
  instance_type = local.gpu_instance_type
}

# The Pricing API is in Virginia; regionCode selects the deployment's rate.
data "aws_pricing_product" "gpu" {
  count        = local.small ? 1 : 0
  provider     = aws.pricing
  service_code = "AmazonEC2"
  filters {
    field = "instanceType"
    value = local.gpu_instance_type
  }
  filters {
    field = "regionCode"
    value = var.region
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

data "aws_ssm_parameter" "gpu_ami" {
  count = !local.small && var.gpu_ami_id == null ? 1 : 0
  name  = "/aws/service/deeplearning/ami/x86_64/base-with-single-cuda-ubuntu-22.04/latest/ami-id"
}

data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
}

locals {
  small             = var.trial_size == "small"
  gpu_instance_type = local.small ? "g6f.large" : var.gpu_instance_type
  model             = local.small ? "llama3.2:3b-instruct-q8_0" : "qwen3:32b"
  required_vram_gb  = local.small ? 3.6 : 20.0
  # GRID reports 3072 MiB nominal memory; use this upper bound when forcing
  # 3/4-node membership, rather than AWS's lower usable-memory specification.
  node_vram_gb    = local.small ? 3.0 : 16.0
  grid_driver_key = "grid-19.5/NVIDIA-Linux-x86_64-580.159.03-grid-aws.run"
  gpu_hourly_usd = local.small ? one(distinct(flatten([
    for term in values(jsondecode(data.aws_pricing_product.gpu[0].result).terms.OnDemand) : [
      for rate in values(term.priceDimensions) : tonumber(rate.pricePerUnit.USD) if rate.unit == "Hrs"
    ]
  ]))) : var.spot_max_price
  az = coalesce(var.availability_zone, data.aws_availability_zones.available.names[0])
  # Include all runtime modules and JSON contracts as the coordinator evolves.
  files = merge({
    for name in setunion(fileset("${path.module}/../../coordinator", "*.py"), fileset("${path.module}/../../coordinator", "schemas/*.json")) :
    name => "${path.module}/../../coordinator/${name}"
    }, {
    "agent.py"         = var.agent_source_path == null ? "${path.module}/snapshots/agent.py" : var.agent_source_path
    "requirements.txt" = "${path.module}/../../coordinator/requirements.txt"
    "launch.py"        = "${path.module}/scripts/launch.py"
    "llama-wrapper.py" = "${path.module}/scripts/llama-wrapper.py"
    "smoke.py"         = "${path.module}/scripts/smoke.py"
  })
  revision = sha256(join("", [for name in sort(keys(local.files)) : filesha256(local.files[name])]))
  shared_config = {
    artifacts         = join(" ", sort(keys(local.files)))
    region            = var.region
    bucket            = aws_s3_bucket.bundle.id
    revision          = local.revision
    max_runtime_hours = var.max_runtime_hours
    llama_cpp_ref     = var.llama_cpp_ref
    ollama_version    = var.ollama_version
    context_size      = var.context_size
    peer_network_mbps = var.peer_network_mbps
    peer_latency_ms   = var.peer_latency_ms
    model             = local.model
    required_vram_gb  = local.required_vram_gb
    node_vram_gb      = local.node_vram_gb
    trial_size        = var.trial_size
    grid_driver_key   = local.grid_driver_key
    cuda_arch         = local.small ? "89" : "75"
    node_count        = var.node_count
  }
}

resource "aws_vpc" "lab" {
  cidr_block           = "10.44.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_subnet" "lab" {
  vpc_id                  = aws_vpc.lab.id
  cidr_block              = "10.44.0.0/24"
  availability_zone       = local.az
  map_public_ip_on_launch = true
  tags                    = { Name = var.name }
}

resource "aws_internet_gateway" "lab" {
  vpc_id = aws_vpc.lab.id
}

resource "aws_route_table" "lab" {
  vpc_id = aws_vpc.lab.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.lab.id
  }
}

resource "aws_route_table_association" "lab" {
  subnet_id      = aws_subnet.lab.id
  route_table_id = aws_route_table.lab.id
}

resource "aws_security_group" "coordinator" {
  name_prefix = "${var.name}-coordinator-"
  vpc_id      = aws_vpc.lab.id
}

resource "aws_security_group" "gpu" {
  name_prefix = "${var.name}-gpu-"
  vpc_id      = aws_vpc.lab.id
}

resource "aws_vpc_security_group_ingress_rule" "api" {
  security_group_id            = aws_security_group.coordinator.id
  referenced_security_group_id = aws_security_group.gpu.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_ingress_rule" "rpc" {
  security_group_id            = aws_security_group.gpu.id
  referenced_security_group_id = aws_security_group.gpu.id
  ip_protocol                  = "tcp"
  from_port                    = 50052
  to_port                      = 50055
}

resource "aws_vpc_security_group_egress_rule" "outbound" {
  for_each          = { coordinator = aws_security_group.coordinator.id, gpu = aws_security_group.gpu.id }
  security_group_id = each.value
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# No SSH, public API, public RPC, NAT gateway, load balancer, or Elastic IP.
resource "aws_s3_bucket" "bundle" {
  bucket_prefix = "${var.name}-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "bundle" {
  bucket                  = aws_s3_bucket.bundle.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "bundle" {
  bucket = aws_s3_bucket.bundle.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "bundle" {
  for_each               = local.files
  bucket                 = aws_s3_bucket.bundle.id
  key                    = "${local.revision}/${each.key}"
  source                 = each.value
  etag                   = filemd5(each.value)
  server_side_encryption = "AES256"
}

resource "random_password" "key" {
  for_each = toset(["admin", "agent", "consumer"])
  length   = 32
  special  = false
}

resource "aws_ssm_parameter" "coordinator_keys" {
  name = "/${var.name}/coordinator-keys"
  type = "SecureString"
  value = jsonencode({
    ADMIN_KEY    = random_password.key["admin"].result
    AGENT_KEY    = random_password.key["agent"].result
    CONSUMER_KEY = random_password.key["consumer"].result
  })
}

resource "aws_ssm_parameter" "agent_key" {
  name  = "/${var.name}/agent-key"
  type  = "SecureString"
  value = random_password.key["agent"].result
}

resource "aws_iam_role" "node" {
  for_each    = toset(["coordinator", "gpu"])
  name_prefix = "${var.name}-${each.key}-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole",
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  for_each   = aws_iam_role.node
  role       = each.value.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "bootstrap" {
  for_each = aws_iam_role.node
  role     = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.bundle.arn}/*" },
      {
        Effect   = "Allow", Action = ["ssm:GetParameter"],
        Resource = each.key == "gpu" ? aws_ssm_parameter.agent_key.arn : aws_ssm_parameter.coordinator_keys.arn
      }
      ], each.key == "gpu" && local.small ? [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "arn:aws:s3:::ec2-linux-nvidia-drivers/${local.grid_driver_key}" }
    ] : [])
  })
}

resource "aws_iam_instance_profile" "node" {
  for_each    = aws_iam_role.node
  name_prefix = "${var.name}-${each.key}-"
  role        = each.value.name
}

resource "aws_instance" "coordinator" {
  ami                                  = nonsensitive(data.aws_ssm_parameter.ubuntu_ami.value)
  instance_type                        = "t3.micro"
  subnet_id                            = aws_subnet.lab.id
  vpc_security_group_ids               = [aws_security_group.coordinator.id]
  iam_instance_profile                 = aws_iam_instance_profile.node["coordinator"].name
  associate_public_ip_address          = true
  instance_initiated_shutdown_behavior = "terminate"
  user_data_replace_on_change          = true
  user_data = templatefile("${path.module}/scripts/bootstrap.sh.tftpl", {
    config_b64 = base64encode(jsonencode(merge(local.shared_config, {
      role          = "coordinator", hostname = "${var.name}-coordinator",
      key_parameter = aws_ssm_parameter.coordinator_keys.name, coordinator = "http://127.0.0.1:8080"
    })))
    runtime_seconds = var.max_runtime_hours * 3600
  })
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 12
    encrypted             = true
    delete_on_termination = true
  }
  credit_specification { cpu_credits = "standard" }
  metadata_options { http_tokens = "required" }
  tags       = { Name = "${var.name}-coordinator" }
  depends_on = [aws_s3_object.bundle, aws_iam_role_policy.bootstrap, aws_iam_role_policy_attachment.ssm, aws_route_table_association.lab]
}

resource "aws_instance" "gpu" {
  count                                = var.node_count
  ami                                  = var.gpu_ami_id != null ? var.gpu_ami_id : (local.small ? nonsensitive(data.aws_ssm_parameter.ubuntu_ami.value) : nonsensitive(data.aws_ssm_parameter.gpu_ami[0].value))
  instance_type                        = local.gpu_instance_type
  subnet_id                            = aws_subnet.lab.id
  vpc_security_group_ids               = [aws_security_group.gpu.id]
  iam_instance_profile                 = aws_iam_instance_profile.node["gpu"].name
  associate_public_ip_address          = true
  instance_initiated_shutdown_behavior = "terminate"
  user_data_replace_on_change          = true
  dynamic "instance_market_options" {
    for_each = local.small ? [] : [1]
    content {
      market_type = "spot"
      spot_options {
        max_price                      = tostring(var.spot_max_price)
        spot_instance_type             = "one-time"
        instance_interruption_behavior = "terminate"
      }
    }
  }
  user_data = templatefile("${path.module}/scripts/bootstrap.sh.tftpl", {
    config_b64 = base64encode(jsonencode(merge(local.shared_config, {
      role          = "gpu", hostname = "${var.name}-gpu-${count.index + 1}",
      key_parameter = aws_ssm_parameter.agent_key.name, coordinator = "http://${aws_instance.coordinator.private_ip}:8080"
    })))
    runtime_seconds = var.max_runtime_hours * 3600
  })
  root_block_device {
    volume_type           = "gp3"
    volume_size           = local.small ? 40 : 80
    encrypted             = true
    delete_on_termination = true
  }
  metadata_options { http_tokens = "required" }
  tags = { Name = "${var.name}-gpu-${count.index + 1}" }
  timeouts { create = "10m" }
  depends_on = [aws_iam_role_policy.bootstrap, aws_iam_role_policy_attachment.ssm]
  lifecycle {
    precondition {
      condition     = data.aws_servicequotas_service_quota.gpu.value >= var.node_count * data.aws_ec2_instance_type.gpu.default_vcpus
      error_message = "The regional G/VT ${local.small ? "On-Demand" : "Spot"} quota is too low. Need ${var.node_count * data.aws_ec2_instance_type.gpu.default_vcpus} vCPUs for quota ${data.aws_servicequotas_service_quota.gpu.quota_code}."
    }
    precondition {
      condition     = local.gpu_hourly_usd > 0 && local.gpu_hourly_usd <= var.spot_max_price
      error_message = "The live GPU rate (${local.gpu_hourly_usd} USD/hour) exceeds the ${var.spot_max_price} USD/hour budget."
    }
  }
}
