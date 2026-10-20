# Coordinator modules and validation

`coordinator.py` is the application entry point. It wires authentication,
persistence, placement and routes, and owns the mutable workload registries,
RPC formation/recovery and consumer lifetimes. Extracted modules do not import
the application entry point or acquire their own copies of its registries.

| Module | Owns | Dependencies |
| --- | --- | --- |
| `api_models.py` | Typed agent, container and media request DTOs | Pydantic, FastAPI validation errors |
| `models.py` | Machine, pending-agent, chat and media job entities | Payload definitions, standard library |
| `security.py` | Authentication and rate limiting | Explicit keys, request metadata |
| `catalog.py` | Bounded model catalogs and memory estimates | No runtime state |
| `placement.py` | Eligibility, context limits, distinct-host GPU selection and queued-demand fairness | Injected inventory, model policies and pending-job reader |
| `persistence.py` | SQLite migrations, recovery and usage queries | Injected database-path provider, entity definitions |
| `admin_api.py` | Inventory, statistics and usage HTTP routes | Injected auth, repository and registry references |
| `validation.py`, `schemas/` | Declarative JSON shape and scalar constraints | Pinned `jsonschema` implementation |
| `chat_admission.py` | Output-cap normalization, tool argument conversion and default language instruction | Validated chat request |
| `chat_stream.py` | Claim-fenced event sequencing, backpressure and SSE normalization | Stream schema, active jobs and agent authentication |

Placement reads the same inventory that the application reserves. Chat, RPC,
media and container admission therefore continue to share physical-host ownership.
Database paths, authentication instances and mutable model estimates remain
isolated between application instances. The existing application-facing helper
names remain available to the scenario test harness.

## JSON contracts

The JSON files use Draft 2020-12. Contracts cover chat requests, incremental
stream chunks, native runtime settings, model-specific runtime profiles, relay
candidates and explicit trial membership. Validators are checked and cached once
per process using the [jsonschema validator API](https://python-jsonschema.readthedocs.io/en/stable/validate/).
Contracts use local definitions and need no remote schema download at runtime.

Structural validation happens before normalization, queue insertion or GPU
reservation. A failed chat request returns HTTP 422 and leaves no job or cluster.
Existing byte limits still reject oversized requests before JSON decoding.
Integer fields retain the prior strict policy: booleans and floats such as `128.0`
are rejected. Error responses report a contract path and rule without echoing
submitted prompt text, token values or credentials.

Pydantic remains responsible for already-typed HTTP payloads. JSON Schema replaces
hand-written type-check chains for the flexible OpenAI-compatible and configuration
objects. Normalization still converts complete JSON tool-argument strings to
objects and preserves incomplete arguments and provider extensions. Missing output
caps retain the existing default; multiple supplied caps use the smallest.

State-dependent decisions remain application policy: authorization, URL safety,
unique relay identities, batch/microbatch compatibility, queue limits, context fit,
route readiness and claim fencing. Valid JSON alone does not authorize or admit work.

## Packaging and checks

The coordinator Docker image includes all Python modules and `schemas/`. The
Terraform GPU lab uploads the same module/contract tree alongside its captured
agent snapshot. Its generated artifact manifest downloads exact objects, including
nested schema paths, using the existing S3 GetObject permission.

Run the [README verification commands](../README.md#verification). The schema tests
exercise malformed inputs, atomic admission, extension/tool compatibility,
credential-safe errors, runtime bounds, role separation and repository isolation.
The broader scenario suite checks scheduling, physical-host reservations,
streaming, teardown and route recovery after this refactor.
