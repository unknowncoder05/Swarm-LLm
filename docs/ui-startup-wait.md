# Chat while distributed agents restart

The dashboard can wait for a catalog distributed model such as
`qwen3:30b-a3b` when its GPU agents are restarting, reconnecting or finishing
other work. A single 12 GiB agent is insufficient for this model's approximately
19 GB combined admission requirement. Previously, the coordinator rejected the
request before forming a cluster, and the UI replaced every HTTP 503 with the
same misleading "No agent available" error.

The coordinator now marks a capacity rejection with
`X-Swarm-Status: waiting_for_capacity` and `Retry-After: 5`. This response means
no inference job or model startup was accepted. The dashboard retains the user's
prompt, shows **Waiting for available GPUs**, and retries that rejection at the
advertised interval for up to 30 minutes. If enough eligible capacity appears,
normal cluster formation, route verification, model loading and inference proceed.
While an accepted request is pending, the UI shows **Waiting for model setup or
response**. It never posts a second request while the accepted one is running.

**Cancel** aborts either the local capacity wait or the active HTTP request.
Leaving the Chat tab also aborts it. Server-side cancellation retains existing
physical GPU reservations until the native role owner acknowledges completion or
teardown. Keep the page open while waiting; the browser's pending request does
not survive a page reload.

Only explicitly marked capacity rejections retry. Authentication failures,
queue/rate limits, model/context errors, unmarked 503s and native inference
errors remain visible with their actual server message. Network failures are
not replayed automatically because acceptance may be unknown. Once the capacity
deadline expires the UI reports a specific timeout. Model choice, reasoning,
context, workload exclusion and GPU admission limits are unchanged.

This is a bounded browser wait, not a durable server-side job submission. The
OpenAI API retains its HTTP 503 capacity response; other clients can use the
same explicit header to decide whether to wait. No cloud resources are created
by this UI behavior. See [architecture](architecture.md) and
[queue durability](queue-durability.md) for persistence boundaries.

Regression coverage includes zero/partial GPU capacity followed by successful
formation, queue saturation, invalid requests, unchanged prompts across retries,
accepted-request nonduplication, cancellation, deadlines and error propagation.
