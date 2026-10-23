# Optional Terraform relay provisioning

Cloud relay use is disabled in the current deployment. Direct LAN QUIC and the
existing coordinator relay do not require a cloud instance. Use the
[routing guide](relay-routing.md) for selection and recovery, and the
[Terraform module](../infra/relay/README.md) for resources, limits and manual use.

The optional private provisioner runs on the existing coordinator host and owns
Terraform execution. The coordinator receives an authenticated relay endpoint,
control key and deadline rather than AWS credentials or a Docker socket.
Consumers cannot choose deployment paths or Terraform inputs.

## Lifecycle and limits

Only eligible multi-machine relay demand can obtain a cloud relay. A single GPU,
explicit direct transport or insufficient GPU capacity does not provision it.
Concurrent demand shares serialized formation and Terraform operations. Capacity
is rechecked after boot before assigning GPUs. Native model traffic goes through
the selected relay; model layers still execute on the GPU agents.

The host service exposes authenticated `POST /ensure`, `PUT /leases/{cluster_id}`
and `GET /status` on a private listener. It validates saved plans against the
module's resource allowlist and bounded instance, disk, price and lifetime
settings. Existing default VPC/subnets and the DNS zone are data sources. The
module creates no additional cloud controller, NAT gateway or load balancer.

Cluster leases last 180 seconds and are renewed while forming, serving or warm.
With no live lease, cleanup waits 300 seconds after demand, checks every
30 seconds and destroys the module's managed resources. The model idle window is
independently configurable. Requests during cleanup either cancel a pending
destroy or wait for a running destroy before recreation. Lease records survive
service restart; Terraform retains its own state locking.

The instance's four-hour termination backstop is a per-instance lifetime, not a
total-spend cap: continued demand may create later instances. New work is rejected
near the relay deadline. Existing native sockets cannot migrate and sufficiently
long requests can be interrupted. Pricing is checked during planning; account
for IPv4, disk, transfer and taxes separately from the compute ceiling.

## Setup and operations

Keep module inputs, state, plans and credentials private. The provisioner uses the
host's existing AWS profile. Choose the bind address from the actual Docker
network; the service must be reachable from the coordinator but not public.

```sh
docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'
python3 scripts/configure-relay-provisioner.py --bind <docker-bridge-gateway>
python3 scripts/configure-relay-provisioner.py --bind <docker-bridge-gateway> --apply
# Review/install the generated unit as a host operator:
sudo install -m 0644 .rpc-trial/relay-provisioner/peerfoundry-relay-provisioner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now peerfoundry-relay-provisioner
# Deploy the coordinator after existing work drains.
```

The helper writes a dedicated provisioner key/URL to `.env`, enables cloud use
and retains `auto` routing. It does not itself apply Terraform or start services.
Check the generated unit's Terraform binary path on hosts without Snap.
Inspect service status and private logs locally; status responses omit the
control key. Terraform errors are retained at
`infra/relay/.provisioner/last-error.log`, and lease state at
`.rpc-trial/relay-provisioner/leases.json`.

To disable and clean up after work finishes:

```sh
python3 scripts/configure-relay.py --disable --apply
# Deploy the cleared coordinator configuration after draining work.
sudo systemctl disable --now peerfoundry-relay-provisioner
terraform -chdir=infra/relay destroy
```

Stop the provisioner before manual Terraform maintenance so demand cannot
recreate resources. Preserve state until cleanup is verified. Disabling cloud
provisioning leaves local routes available. Dated deployment and cost observations
are retained locally in ignored `.journal/` files.
