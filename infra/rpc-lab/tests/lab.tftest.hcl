mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a"] }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-00000000000000000" }
  }
  mock_data "aws_servicequotas_service_quota" {
    defaults = { value = 32 }
  }
  mock_data "aws_ec2_instance_type" {
    defaults = { default_vcpus = 4 }
  }
}
mock_provider "random" {}
mock_provider "aws" { alias = "pricing" }

variables { trial_size = "large" }

run "default_disposable_topology" {
  command = apply

  assert {
    condition     = length(aws_instance.gpu) == 2 && aws_instance.coordinator.instance_type == "t3.micro"
    error_message = "The default trial must contain two GPUs and one tiny coordinator."
  }
  assert {
    condition     = alltrue([for node in aws_instance.gpu : node.instance_market_options[0].market_type == "spot" && node.instance_market_options[0].spot_options[0].spot_instance_type == "one-time" && tonumber(node.instance_market_options[0].spot_options[0].max_price) == 0.30])
    error_message = "GPU nodes must use capped, one-time Spot requests."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.rpc.referenced_security_group_id == aws_security_group.gpu.id && aws_vpc_security_group_ingress_rule.rpc.from_port == 50052 && aws_vpc_security_group_ingress_rule.rpc.to_port == 50055 && aws_vpc_security_group_ingress_rule.api.referenced_security_group_id == aws_security_group.gpu.id
    error_message = "RPC and API ingress must be restricted to private lab GPU peers."
  }
  assert {
    condition     = alltrue([for node in concat(aws_instance.gpu, [aws_instance.coordinator]) : node.instance_initiated_shutdown_behavior == "terminate" && node.root_block_device[0].delete_on_termination && node.root_block_device[0].encrypted && node.metadata_options[0].http_tokens == "required"])
    error_message = "Termination must delete encrypted root disks; metadata must require IMDSv2."
  }
  assert {
    condition     = aws_s3_bucket.bundle.force_destroy && aws_s3_bucket_public_access_block.bundle.block_public_policy && output.lab.max_gpu_hourly_usd == 0.60
    error_message = "Source artifacts must clean up and the default GPU cap must remain 0.60 USD/hour."
  }
  assert {
    condition     = alltrue([for node in concat(aws_instance.gpu, [aws_instance.coordinator]) : strcontains(node.user_data, "OnBootSec=7200s") && length(node.user_data) < 16384])
    error_message = "Every user-data script must arm the two-hour timer and fit EC2's size limit."
  }
}

run "three_node_trial" {
  command = apply
  variables { node_count = 3 }
  assert {
    condition     = length(aws_instance.gpu) == 3 && format("%.2f", output.lab.max_gpu_hourly_usd) == "0.90"
    error_message = "Three-node experiments must create exactly three capped GPU instances."
  }
}

run "reject_unbounded_node_count" {
  command = plan
  variables { node_count = 5 }
  expect_failures = [var.node_count]
}

run "reject_unbounded_runtime" {
  command = plan
  variables { max_runtime_hours = 24 }
  expect_failures = [var.max_runtime_hours]
}

run "reject_missing_gpu_quota_before_creation" {
  command = plan
  override_data {
    target = data.aws_servicequotas_service_quota.gpu
    values = { value = 0 }
  }
  expect_failures = [aws_instance.gpu]
}

run "small_fractional_real_gpu_trial" {
  command = apply
  variables { trial_size = "small" }
  override_data {
    target = data.aws_servicequotas_service_quota.gpu
    values = { value = 4 }
  }
  override_data {
    target = data.aws_ec2_instance_type.gpu
    values = { default_vcpus = 2 }
  }
  override_data {
    target = data.aws_pricing_product.gpu[0]
    values = { result = "{\"terms\":{\"OnDemand\":{\"test\":{\"priceDimensions\":{\"hour\":{\"unit\":\"Hrs\",\"pricePerUnit\":{\"USD\":\"0.202\"}}}}}}}" }
  }
  assert {
    condition     = alltrue([for node in aws_instance.gpu : node.instance_type == "g6f.large" && length(node.instance_market_options) == 0 && node.root_block_device[0].volume_size == 40])
    error_message = "The small profile must use two fractional real GPUs on explicit On-Demand with smaller disks."
  }
  assert {
    condition     = output.lab.model == "llama3.2:3b-instruct-q8_0" && format("%.3f", output.lab.max_gpu_hourly_usd) == "0.404" && data.aws_servicequotas_service_quota.gpu.quota_code == "L-DB2E81BA"
    error_message = "Small trials must test the Q8 model with actual On-Demand pricing and quota."
  }
}

run "reject_small_gpu_rate_above_budget" {
  command = plan
  variables { trial_size = "small" }
  override_data {
    target = data.aws_pricing_product.gpu[0]
    values = { result = "{\"terms\":{\"OnDemand\":{\"test\":{\"priceDimensions\":{\"hour\":{\"unit\":\"Hrs\",\"pricePerUnit\":{\"USD\":\"0.9\"}}}}}}}" }
  }
  expect_failures = [aws_instance.gpu]
}
