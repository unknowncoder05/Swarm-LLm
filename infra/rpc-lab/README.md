# Disposable AWS RPC lab

This Terraform root module runs the repository's coordinator and Python agent
snapshot on real AWS machines. The default is two `g6f.large` On-Demand instances
(one fractional NVIDIA L4 with about 2.79 GiB VRAM each) and a `t3.micro`
coordinator in one subnet and AZ. Llama 3.2 3B Q8_0's 3.4 GB weights exceed either
GPU alone. This is a smaller-scale test of the same model-splitting mechanism.
The optional `large` profile uses two `g4dn.xlarge` Spot instances (16 GiB T4 each)
with Qwen3 32B's roughly 20 GB Q4_K_M weights. The actual
Swarm request path forms the cluster and returns two short chat responses.

The lab owns its VPC, subnet, internet gateway, routing, security groups, IAM
roles/profiles/policies, generated API keys and SSM parameters, private S3 source
bundle, EC2 instances, and their root disks. There are no manually provisioned
cloud dependencies beyond your existing AWS account and credentials. The normal
runner destroys the complete lab even after an inference or bootstrap failure.

Dated GPU allocations, trial results and timings are retained in ignored
`.journal/` files. The runner verifies capacity, native placement and responses
before destroying the lab; the [inference guide](../../docs/distributed-inference.md)
describes the application path under test.

## Cost and fidelity

- Choose the market explicitly with `trial_size`: `small` uses On-Demand;
  `large` uses capped, one-time Spot requests. Neither falls back to the other.
  Terraform checks the corresponding regional G/VT quota before creation:
  `L-DB2E81BA` for On-Demand (4 vCPUs for two small nodes), or `L-3819A6DF`
  for Spot (8 vCPUs for two T4 nodes). The quota check uses the approved regional
  limit; other running instances can consume that allowance and make launch fail.
  It also checks the small profile's live Linux On-Demand price against
  `spot_max_price`, the shared GPU hourly budget.
  This planning guard is not an AWS billing cap; a subsequent pricing change can
  change On-Demand charges. The Spot profile submits that budget as its EC2 cap.
- On 2026-10-09 the account had zero G Spot quota in **all 17 enabled regions**,
  and zero G On-Demand quota outside Virginia. Virginia (`us-east-1`) had
  4 G On-Demand vCPUs, enough for two `g6f.large` nodes. Offering zones were
  `us-east-1a`, `1b`, `1c`, and `1d` in this account. Quota and an instance-type
  offering do not guarantee launch capacity. This lab does not change quotas,
  subscribe to Marketplace images, or reserve capacity.
- Every instance arms a two-hour shutdown timer **before** dependency installation.
  EC2 is configured to terminate on shutdown; root disks delete on termination.
  This is a best-effort compute limit, not a billing guarantee or replacement for
  `terraform destroy`. A hung guest or failed user-data execution can defeat it.
- The verified small-profile GPU rate was **$0.202/node-hour**, **$0.404/hour
  combined**, or $0.808 for two hours, from the AWS Pricing API on 2026-10-09.
  Allow roughly $0.45/hour total in Virginia at those rates, before billable
  transfers and taxes. The coordinator, 92 GB total gp3 storage while running,
  three public IPv4 addresses,
  S3 requests/storage, and any billable transfers are additional. There is no NAT
  gateway, load balancer, reserved capacity, EIP, or paid monitoring service.
- The large-profile default GPU compute ceiling is $0.60/hour, or $1.20 for two
  hours, plus the same extras with 172 GB gp3 storage. Live Spot quotes on
  2026-10-09 were approximately $0.269/GPU-hour in
  `us-east-1f` and $0.290 in `us-east-1b`. Prices change, AZ names are account-specific,
  and a low cap can cause launch failure. Check before applying:

  ```bash
  aws ec2 describe-spot-price-history --region us-east-1 \
    --instance-types g4dn.xlarge --product-descriptions Linux/UNIX \
    --start-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --query 'SpotPriceHistory[].{AZ:AvailabilityZone,USD:SpotPrice}'
  ```

  Check the small-profile quota with (use `L-3819A6DF` for the large profile):

  ```bash
  aws service-quotas get-service-quota --region us-east-1 \
    --service-code ec2 --quota-code L-DB2E81BA \
    --query 'Quota.{Name:QuotaName,ApprovedLimit:Value}'
  ```

- Traffic between GPU machines uses private TCP, capped by default at 1 Gbps
  with 1 ms additional egress delay on each GPU node. Internet downloads are not
  shaped. Set `peer_network_mbps = 0` for the unshaped AWS network. This approximates
  a wired LAN; fractional L4s/T4s differ from the project's RTX 4070/5070 machines,
  and this does not reproduce university firewalls or Windows startup behavior.
- Both nodes download the model, so either can become primary. They compile the
  same pinned CUDA/RPC llama.cpp revision. Compilation and downloads count toward
  the run-time budget; allow roughly 20–75 minutes for preparation, depending on
  mirrors, build time, and instance load. No prebuilt image is retained afterward.
- Fractional G6f GPUs require **NVIDIA GRID**, not the Tesla driver in a standard
  Deep Learning AMI. The small profile uses ordinary Ubuntu 22.04 and installs
  AWS's pinned GRID 19.5 driver plus CUDA Toolkit 12.8, targeting architecture 89.
  Its instance role can read only that driver object from AWS's driver bucket.
  CUDA Toolkit installation deliberately excludes replacement GPU drivers.
  Check bootstrap and native device logs on both GPUs during each trial.

## Run a complete disposable trial

Requirements: Terraform >= 1.10, AWS CLI, Python >= 3.8, AWS credentials with
permissions to manage these resources and use SSM Run Command. No SSH keys or
Session Manager browser plugin are needed for the automated runner.
Allow roughly 2 GiB of free local disk space for Terraform's provider download
and installation; this is separate from the EC2 disks.

From this directory:

```bash
cp terraform.tfvars.example terraform.tfvars
# Review region, AZ, GPU count, hourly cap, and run-time limit in terraform.tfvars.
python3 trial.py
```

For the original 32B test, copy `large.tfvars.example` instead. It needs an
approved G Spot quota; the reviewed account cannot currently launch that profile.

The runner initializes Terraform and calls `terraform apply`, which displays the
resource plan and asks for approval before creating paid instances. It then waits
for cloud-init and services, invokes the smoke test through SSM on the coordinator,
saves results and machine logs to `results.json`, and runs
`terraform destroy -auto-approve` in a `finally` block. This directory must have a
dedicated lab state: cleanup destroys everything managed by that state.

To use an already reviewed saved plan, run `terraform plan -out=trial.tfplan`
then `python3 trial.py --apply-plan trial.tfplan`. Applying that saved plan has
no second confirmation prompt. Keep its variable configuration in
`terraform.tfvars` so the runner's subsequent destroy uses the same settings.

The test requires all requested agents to be online, verifies that the large
model exceeds one GPU's reported memory, checks model advertisement, sends a cold
and a warm request, and verifies an active cluster containing all trial members.
The report includes replies, token usage, request times, source revision, device
logs, GPU memory snapshots, and llama.cpp/RPC logs. Confirm that the primary's
logs show CUDA and remote RPC allocations; the runner also requires an active
llama-server or RPC process using at least 1 GiB on every GPU machine. An API
success alone is insufficient evidence of real GPU distribution. These two requests are a smoke test, not a
performance benchmark.

Spot interruption fails the experiment; there is no automatic replacement fleet.
After an interrupted run, the runner still attempts Terraform cleanup. Do not
delete the state before cleanup succeeds.

## Manual operation and inspection

```bash
terraform init
terraform validate
terraform plan
terraform apply
terraform output -json lab
python3 trial.py --test-existing --keep
# When finished:
terraform destroy
terraform state list
```

`--keep` retains the lab for inspection, including after failures. The guest timer
still applies. Without `--keep`, `--test-existing` destroys the existing lab at
the end. `terraform state list` should be empty after successful destruction.
The runner handles Ctrl-C, SIGTERM, and terminal hangup by attempting cleanup.
If it is forcibly killed or cannot reach AWS, immediately rerun
`terraform destroy`; the guest timer terminates compute only, leaving the tiny
source bucket and IAM/SSM/network resources for Terraform to remove.

For an individual machine, use the ID from the `lab` output:

```bash
aws ssm start-session --region us-east-1 --target INSTANCE_ID
```

Inside a session:

```bash
sudo tail -50 /var/log/swarm-bootstrap.log
sudo journalctl -u swarm-lab --no-pager -n 60
sudo nvidia-smi
sudo tail -80 /var/log/swarm-llama.log
sudo tail -40 /var/log/swarm-rpc.log
sudo systemctl list-timers swarm-expire.timer
```

Interactive sessions require the AWS Session Manager plugin. Only private GPU
peers can reach RPC ports `50052–50055`; only GPU nodes can reach coordinator
port `8080`. All administrative access uses SSM. Public IPs enable outbound
package/model downloads without a NAT gateway; there is no public ingress.

## Deliberate adaptations to the application

The uploaded `coordinator.py` comes directly from this checkout. The agent is
captured in [snapshots/agent.py](snapshots/agent.py), because `agent/` is a
separate Git checkout ignored by the main repository. Its reviewed working tree
includes RPC code absent from its base commit, so fetching that base alone
would not reproduce this experiment. See [snapshot provenance](snapshots/README.md).
Set `agent_source_path` to a reviewed local agent file to test a newer version.
Both source content hashes contribute to the recorded source revision.
Production files are not rewritten. The experiment applies a small adapter in
`launch.py` and process wrappers:

1. Resolve llama.cpp to the locally compiled, pinned binaries. Bridge the agent's
   `llama-rpc-server` name to upstream's `ggml-rpc-server`. Install Ollama under
   `/opt/swarm/ollama` so its bundled server lookup does not select the separate
   RPC wrapper. Check Ollama startup and prepare the model before compiling.
   Give cloud-init the root user's normal home directory: Ollama panics when
   cloud-init's sparse environment omits `HOME`.
2. Resolve the explicitly configured Ollama store and disable agent self-updates
   so the experiment stays on its uploaded snapshot. Disable media polling to
   keep GPU allocation specific to the distributed LLM trial.
3. Use mmap loading, one inference slot, Flash Attention, q8 KV cache, a default
   1K context (8K in the large example), a 256-token generation cap, and a
   15-minute first-load wait. These
   settings reduce RAM/VRAM pressure on inexpensive nodes and retain device logs.
   Use a 128-token batch and 64-token microbatch to bound device scratch buffers.
   Replace the agent's forced 999 GPU layers with automatic memory fitting and
   reserve 512 MiB per GPU for CUDA/cuBLAS allocations made during inference.
   Inspect the captured offload count: fitting can leave layers on the CPU.
   Pin the compiled GPU architecture and limit generated Flash Attention vector
   variants to q8/q8, matching the KV cache. Both machines use the same source tag.
   An 8 GiB swap reserve protects transient host allocations; sustained swapping
   would distort performance measurements and should be checked during inspection.
4. Add `llama3.2:3b-instruct-q8_0` to only this lab's RPC catalog with a 3.6 GB admission
   estimate, or keep the 20 GB estimate for `qwen3:32b` in the large profile.
   These estimates use the application's GB convention and include an approximate
   allowance; exact CUDA allocation is checked in the captured logs.
   GRID reports 3,072 MiB nominal device memory; its usable memory is lower than
   this total. The membership calculation uses the nominal 3 GiB upper bound.
   For an explicit
   three/four-node experiment, increase that one lab model's estimate to force
   all requested nodes into the cluster. This demonstrates extra workers; it does
   not imply that the same weights require that many GPUs. Do not increase the
   node count beyond your region's quota.
5. Exclude the test model from the agent's ordinary Ollama queue selection. The
   production agent assumes a minimum 6 GB budget even on a smaller GPU; without
   this adapter it could race the RPC polling thread and serve the 4B job itself.
   Hardware memory reporting and the actual coordinator RPC path remain in use.

Use `context_size = 16384` to match the production-assigned context only on
devices with sufficient free memory. The small profile defaults to 1024: the
earlier Qwen3 4B Q8 G6f trial exhausted device memory allocating the KV cache
at 4096, and a 1K retry without a runtime reserve failed in cuBLAS.
The model tags are
`llama3.2:3b-instruct-q8_0` or `qwen3:32b`; Ollama tags can move. The model manifest/blob hashes are present in
bootstrap/agent logs. For strict artifact reproducibility, record the blob digest
and retain the model externally before changing this lab to a different artifact.

Changing uploaded sources or configuration replaces instances through
`user_data_replace_on_change`. The GPU AMI defaults to AWS's current Deep Learning
Base Ubuntu 22.04 CUDA image in the large profile, or plain Ubuntu in the small
profile; set `gpu_ami_id` to pin a compatible image for repeated runs. A small
profile override must permit installation of the GRID driver.
This experiment uses the application's real scheduling and inference logic but
does not fix its broader failure-recovery and capacity-accounting limitations.
See [the application review](../../docs/distributed-inference.md).

## Validation without creating resources

```bash
terraform init -backend=false
terraform fmt -check -recursive
terraform validate
terraform test
python3 -m unittest discover -s tests -v
```

Terraform tests use mocked providers and exercise topology, cost limits, private
ingress, and disk cleanup settings. Python tests cover the adapters, multi-machine
checks, and runner cleanup after failure. Passing these tests does not establish
that EC2 GPU bootstrap or inference has succeeded; only a real trial does that.

Terraform state contains generated API keys even though they are sensitive
values and are not exposed in outputs. Keep state and result files private. The
S3 bucket contains application source only, is access-blocked and encrypted, and
is emptied during destruction. No snapshots or log groups are created.

## References

- [AWS G4 instance specifications](https://aws.amazon.com/ec2/instance-types/g4/)
- [AWS G6 fractional GPU specifications](https://aws.amazon.com/ec2/instance-types/g6/)
- [AWS GRID driver requirements and installation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/nvidia-GRID-driver.html)
- [Llama 3.2 3B Q8_0 artifact](https://ollama.com/library/llama3.2:3b-instruct-q8_0)
- [AWS public DLAMI lookup](https://docs.aws.amazon.com/dlami/latest/devguide/aws-deep-learning-x86-base-with-single-cuda-ami-ubuntu-22-04.html)
- [Terraform EC2 Spot configuration](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/instance#spot-options)
- [llama.cpp RPC build and network requirements](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md)
- [Pinned llama.cpp server options](https://github.com/ggml-org/llama.cpp/blob/v0.6.0/tools/server/README.md)
- [Qwen3 32B weights and quantization](https://ollama.com/library/qwen3:32b)
