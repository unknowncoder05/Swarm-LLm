# Controller model and replica status

The Chat tab [waits for distributed GPU capacity during agent restarts](ui-startup-wait.md),
then keeps the request pending through model setup. It offers cancellation and
preserves actual server errors instead of replacing every 503 with "No agent available".

The Machines tab refreshes `/machines` every three seconds. Its model column
uses runtime and cluster reports rather than treating an agent's configured
default model as a resident model.

- **Ollama:** all names in `runtime.ollama_models` are shown as loaded. An empty
  list means no model is loaded; a missing report means the state is unavailable.
  The configured default is labeled separately. Offline rows show historical
  model reports.
- **Distributed inference:** a reserved machine is matched to its replica by
  agent ID, never by hostname. The row shows the actual distributed model,
  primary/worker role, replica ID, connection route and context size. The model
  remains a loading target until the cluster is active and its primary is ready.
  A ready worker is labeled as an RPC worker rather than an available idle GPU.
- **Downloads:** an agent in `DOWNLOADING` with a valid 0–100 percentage in its
  detail report gets an accessible progress bar. Otherwise the UI says that
  progress was not reported. This includes model pulls that have not yet emitted
  a percentage. A stale offline report does not show an active download bar.
- **RPC tensor transfer:** forming QUIC replicas show actual bytes sent by the
  primary or received by the worker. These counters include RPC traffic and do
  not provide a reliable total model download size, especially after cached
  tensors are reused. They are not converted into a percentage.
- **GPU memory:** the reported allocation is labeled as memory in GiB. It is
  not a CUDA compute-utilization measurement.

Replica cards above the table show model, member hostnames, context, connection
route and loading/ready/serving/releasing state. Unavailable members prevent a
card from claiming readiness. Teardown reasons remain visible while the
coordinator retains the dissolving cluster. A regional relay is not assigned a
geographic label solely from its URL.

An API refresh failure is shown in the header, with the last successful update
time. Existing values remain the last reported snapshot.

## Validation and deployment

```sh
npm test --prefix ui
npm run build --prefix ui
docker compose build ui
docker compose up -d --no-build --no-deps ui
docker compose exec -T gateway nginx -s reload
```

UI tests exercise download bounds, unavailable telemetry, stale offline
reports, reused hostnames, worker-versus-replica readiness, real loaded model
names, transfer counters, accessible progress bars, failed teardown and relay
labels. Tests render components using React's server renderer and compile their
TypeScript into a temporary directory; they add no testing dependencies.
Chat request tests cover restart waits, accepted-request nonduplication,
cancellation, deadlines and propagation of actual server errors.

Only the UI container is replaced for this deployment. Reloading the gateway
refreshes the UI's Docker address; the coordinator and native model processes
continue running.
