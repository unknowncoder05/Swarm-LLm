output "lab" {
  value = {
    region             = var.region
    availability_zone  = local.az
    coordinator_id     = aws_instance.coordinator.id
    coordinator_ip     = aws_instance.coordinator.private_ip
    gpu_ids            = aws_instance.gpu[*].id
    gpu_private_ips    = aws_instance.gpu[*].private_ip
    key_parameter      = aws_ssm_parameter.coordinator_keys.name
    node_count         = var.node_count
    model              = local.shared_config.model
    source_revision    = local.revision
    trial_size         = var.trial_size
    gpu_instance_type  = local.gpu_instance_type
    gpu_market         = local.small ? "on-demand" : "spot"
    max_gpu_hourly_usd = var.node_count * local.gpu_hourly_usd
    max_runtime_hours  = var.max_runtime_hours
  }
}
