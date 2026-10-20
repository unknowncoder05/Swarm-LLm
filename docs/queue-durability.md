# Queue durability and RabbitMQ

Status: design evaluated; no broker deployed or infrastructure added.

## Decision for the current two-GPU deployment

Keep the bounded in-process queue while hardening scheduling and request
lifetimes. RabbitMQ would improve durable delivery and decouple dispatch from
the API process; it would not accelerate native model decoding. Persistent job
and worker-lease state is the prerequisite for a useful migration. Replacing
`asyncio.Queue` alone leaves coordinator recovery and lost client responses
unsolved.

The existing API is synchronous. A disconnected HTTP connection cannot survive
a coordinator restart, even if its message survives in a broker. Add durable
job IDs, idempotent submission, and a result/status endpoint so clients can
reattach before claiming transparent recovery.

## Proposed boundaries

Keep HTTP routes responsible for authentication and request/response handling.
The current `chat_admission.py` owns input normalization without I/O. Extract
these additional boundaries when implementing durable execution:

| Component | Responsibility |
| --- | --- |
| Job store | Persist request, deadline, result, cancellation, and attempt state |
| Lease store / scheduler | Atomically reserve one physical host across every workload and fence attempts |
| Dispatch queue adapter | Notify scheduling lanes of pending job IDs; memory or RabbitMQ implementation |
| Outbox publisher | Publish committed transitions with confirms and retry interrupted publication |
| Agent transport | Retain outbound HTTPS polling and authenticated result upload |

Use the existing SQLite volume for a first single-coordinator implementation.
Persist jobs, leases, and an outbox in one transaction. If multiple coordinators
are later required, choose a shared transactional store and test atomic claims;
a RabbitMQ cluster does not make a local SQLite file shared or highly available.

## RabbitMQ integration requirements

Use persistent messages, durable queues, publisher confirms, and manual consumer
acknowledgments. Acknowledge after a durable scheduling handoff or terminal
state is committed. Retries can redeliver, so job IDs and attempt fencing remain
authoritative. Bound consumer prefetch; it bounds pushed deliveries, whereas
polling with `basic.get` needs its own outstanding-work bound.
[RabbitMQ acknowledgment and prefetch semantics](https://www.rabbitmq.com/docs/confirms).

Preserve model/context eligibility and a single physical GPU lease across chat,
RPC, media, and container work. A dispatch lane must reserve a host before it
hands work to that host. Prefetch is an intake control, not the GPU lock. Keep
AMQP internal to the coordinator network; campus agents retain their existing
outbound HTTPS connection and need no administrator access or broker credentials.

Notifications should contain job IDs and scheduling metadata, not model weights
or RPC tensors. Retain the current application limits of eight waiting jobs per
model and 64 globally. Configure broker byte/count bounds with publication
rejection, and expire or dead-letter poison work with bounded retries. Check
the stored deadline and cancellation state again at dispatch and result commit.
[RabbitMQ queue limits](https://www.rabbitmq.com/docs/queues).

Broker requeue after a disconnect is not evidence that an old GPU computation
stopped. Preserve its physical reservation until a matching terminal upload or
confirmed teardown/restart. Reject a previous attempt's result after another
attempt acquires the logical job. Repeated notifications must not duplicate
execution on one host or overwrite a committed result.

For a budget trial, a single broker on the existing coordinator with a separate
persistent volume would test broker restart recovery, but would share that
host's failure domain. A practical highly available quorum queue starts with
three broker nodes; that is a different resource budget.
[RabbitMQ quorum sizing](https://www.rabbitmq.com/docs/quorum-queues#group-size).
Do not put durable queue data on the relay: its four-hour termination
timer deletes its root disk. Any new cloud infrastructure must have a reviewed
Terraform plan and cleanup path.

## Migration acceptance scenarios

1. Stop/restart the broker before and after publisher confirmation; recover
   accepted jobs without accepting publication that was actually rejected.
2. Crash the coordinator between database commit, publication, host reservation,
   HTTP delivery, and result acknowledgment. Recover through the outbox and
   persisted leases, with one committed terminal result per job.
3. Partition an agent while its native process keeps running. Deliver duplicate
   notifications and late results; retain the old physical GPU reservation.
4. Cancel or expire jobs at every stage. Neither restart nor redelivery may
   revive cancelled work, and dead letters must remain bounded and inspectable.
5. Fill model/global queues and broker storage. Return explicit backpressure;
   do not reserve GPUs or drop accepted jobs silently.
6. Add, remove, or restart machines during dispatch. Preserve model/context
   eligibility, stable identities, and cross-workload exclusion.

These scenarios are migration criteria, not claims that broker recovery is
implemented today. See [current scheduling boundaries](architecture.md#scheduling-and-resource-ownership).
