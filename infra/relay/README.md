# RPC relay trial

Currently disabled and destroyed. The reusable module is retained; follow the
[current routing and recovery guide](../../docs/relay-routing.md) for status,
configuration and future enablement. Dated trial details are retained in ignored
`.journal/`; this document covers module operation.

The [coordinator-triggered lifecycle](../../docs/relay-provisioning.md)
can now create this module when a multi-GPU request needs it and destroy all its
managed resources after idle use. It uses the existing coordinator host for the
private Terraform provisioner; it creates no additional AWS controller resources.
Manual trial deployment below remains available when that service is disabled.

One CPU-only EC2 instance in **Europe (Spain), `eu-south-2`**, forwards native
RPC bytes between the existing Windows GPU agents. Scheduling, job results,
the dashboard, and model inference remain on the existing coordinator/agents.
The GPUs connect directly to the relay HTTPS endpoint for model traffic.
The regional endpoint does not forward those bytes through Virginia.

## Resources and cost

Terraform reads the account's **default VPC and default public subnet** and the
existing public DNS zone supplied as `hosted_zone_name`. It creates one `t4g.micro` with a normal
public IPv4 address, an encrypted 8 GB gp3 root disk, one security group and its
rules, an SSM SecureString, the instance's IAM/SSM permissions, and one DNS record.
It does not create a VPC, subnet, route table, NAT gateway, load balancer,
Elastic IP, GPU node, S3 bucket, or additional hosted zone.

The live EC2 Pricing API returned **$0.0092/hour** for Linux `t4g.micro` on
2026-10-16. Terraform reads this rate during planning and rejects compute above
the configured ceiling (at most $0.02/hour). CPU credits use **standard** mode
to avoid surplus-credit charges. Increase to `t4g.small` only if measurements
show CPU throttling or insufficient memory.

[Public IPv4 costs $0.005/hour](https://aws.amazon.com/vpc/pricing/), in addition
to compute. The 8 GB root disk and any billable outbound traffic are separate.
Compute plus IPv4 for the default four-hour trial is **$0.0568**, before disk,
traffic, and taxes. The earlier cold load relayed about 9.4 GB; cached tensors
may reduce a repeat load, but do not assume the account's shared internet-transfer
allowance is unused. See [EC2 transfer pricing](https://aws.amazon.com/ec2/pricing/on-demand/).

A systemd timer is armed before bootstrap downloads. By default it **terminates
the instance four hours after boot**. The root disk is deleted and the ordinary
public IPv4 address is released. DNS, IAM, security-group, and SSM resources stay
tracked until `terraform destroy`. The timer is a guest-side backstop, not a
guaranteed total-spend cap or a substitute for Terraform cleanup.

HTTPS is restricted to the worker network and coordinator source CIDRs. Port 80 is open
for ACME certificate validation and HTTPS redirects. The native RPC and Python
listeners bind only to loopback; management uses outbound SSM without SSH.
The agent key authenticates data requests; a distinct generated control key
authenticates cluster registration, discovery, lease renewal, and deletion.
Keys are fetched from SSM at bootstrap and are absent from EC2 user-data.

## Deploy

If Spain is disabled, enable it with the small separate Terraform state first:

```sh
umask 077
terraform -chdir=infra/relay/region init
terraform -chdir=infra/relay/region plan -out=region.tfplan
terraform -chdir=infra/relay/region apply region.tfplan
```

Copy `terraform.tfvars.example` to a private ignored `terraform.tfvars` and
supply the existing agent key, actual public source CIDRs, `hosted_zone_name`
(for example `example.com`) and `dns_name` (for example `relay.example.com`).
The DNS name must be one subdomain of that existing zone. If an ignored
`terraform.auto.tfvars.json` already exists, it is the current private input;
avoid keeping two conflicting input files. Terraform state and saved plans
contain credentials and must also remain private.

```sh
umask 077
terraform -chdir=infra/relay init
terraform -chdir=infra/relay validate
terraform -chdir=infra/relay plan -out=relay.tfplan
terraform -chdir=infra/relay apply relay.tfplan
terraform -chdir=infra/relay output deployment
```

Wait for cloud-init, the relay service, DNS, and the public ACME certificate.
The expected endpoint is `https://relay.example.com/healthz`.
Inspect failures through SSM and `/var/log/cloud-init-output.log`,
`journalctl -u peerfoundry-relay -u caddy`, and `systemctl list-timers`.

## Connect and verify

The reviewed agent supports a per-assignment regional URL. Updated agents report
`runtime.rpc_relay_external=true`; the coordinator excludes older agents from
regional clusters. Local relay and direct transport retain their existing paths.

```sh
python3 scripts/configure-relay.py
python3 scripts/configure-relay.py --apply
# Wait for active requests to finish before restarting the coordinator.
docker compose up -d --build coordinator
python3 scripts/agent-health.py --hosts GPU-NODE-01 GPU-NODE-02
python3 scripts/rpc-trial.py --transport relay --hosts GPU-NODE-01 GPU-NODE-02
```

For a coding session, also hold the native model for 15 minutes between requests:

```sh
python3 scripts/configure-relay.py --apply --idle-timeout-seconds 900
```

Both GPUs remain reserved during that window. This does not extend
the Terraform relay's runtime limit; idle regional clusters drain five minutes
before expiry, and new regional requests receive a retry response near that
deadline. See the [provisioner lifecycle guide](../../docs/relay-provisioning.md).

The configuration helper verifies instance state, HTTPS, and control credentials,
then writes settings privately without printing credentials. It also sets
`RPC_DEFAULT_TRANSPORT=relay`, including when disabling the regional endpoint,
so this setup uses outbound connections without Windows administrator access.
Explicit request transport settings still override this default. It derives the
expiry from EC2's launch time and the Terraform runtime limit. New clusters fall
back to the local relay within five minutes of that deadline. Existing regional
clusters cannot migrate their live socket state at termination; finish requests
before the limit. Changing `max_runtime_hours` replaces the instance.

The coordinator registers allowed members before releasing assignments. It polls
only channel IDs and byte counters from Spain, keeping tensor frames off the
Virginia path. A 180-second lease removes orphaned regional state when renewal
stops. Dissolution closes local sockets and deletes remote state; expired leases
and application shutdown also wake blocked senders/receivers and clear queues.

Use the same model, context, machines, and cold/warm prompts for
[performance comparisons](../../docs/performance.md). Only successful responses with native
process/GPU placement evidence establish that the regional trial works.

## Cleanup

If automatic provisioning is enabled, follow its [disable and cleanup instructions](../../docs/relay-provisioning.md#setup-and-operations)
first, so a new request cannot recreate resources during manual maintenance.

After requests finish, restore the local relay configuration and destroy this
module's complete state:

```sh
python3 scripts/configure-relay.py --disable --apply
docker compose up -d coordinator
terraform -chdir=infra/relay destroy
terraform -chdir=infra/relay state list
```

The default VPC/subnets and pre-existing DNS zone are data sources and survive
cleanup. Region activation is tracked separately and carries no hourly charge;
this module does not disable the account's Spain region during trial cleanup.
Do not delete either state file before its managed resources have been reviewed.
