# Security and publication review

PeerFoundry is intended for a trusted group of machines operated by an individual
or institution. It is not a tenant-isolation platform for mutually untrusted users.

## Review completed on 2026-10-23

This review covered current publishable files, API role checks, agent updates,
RPC transport, container execution, gateway exposure and dependency advisories.
It was a source review with automated checks, not a penetration test or security
certification.

Changes made during the review:

- Removed chat prompt previews from coordinator admission logs.
- Reject empty or duplicated admin, agent and consumer credentials so a consumer
  key cannot accidentally acquire administrator permissions through configuration.
- Store the dashboard admin credential in session storage, removing old persistent
  admin keys when the dashboard opens. Closing the tab ends that stored session.
- Create fallback agent-update files atomically instead of choosing an unreserved
  temporary filename.
- Updated FastAPI/Starlette and Vite dependencies to resolve the advisories
  reported by `pip-audit` and `npm audit`; use `npm ci` for dashboard image builds.
- Expanded ignore rules for credentials, cloud configuration, Terraform state and
  plans, and local databases. Added a repeatable publication scan.

The updated coordinator requirements and dashboard lockfile reported no known
vulnerabilities in their dependency audits on the review date. This does not audit
OS images, native GPU binaries, optional media packages, or every dependency an
agent may install. Advisory databases and software versions change over time.

Validation completed: 331 coordinator/agent tests, 22 dashboard tests, 12
infrastructure tests, dashboard production build, isolated Nginx configuration
validation, Terraform formatting and local validation of both Terraform modules.
No cloud resources were planned or applied during this review.

## Publication privacy

The current source scan found no known personal/university identifiers, deployment
domains, or credential signatures. Private IP addresses in tests are synthetic
fixtures for private-network routing; infrastructure contains generic network
configuration. Setup prose uses documentation addresses and operator placeholders.

Run `python3 scripts/audit-publication.py` before publication. Supply extra
`--forbid` values privately for any local domain, institution name, hostname or
address that must not appear. Findings show filenames, line numbers and categories,
not matched values. This is a pattern-based check, not proof that every possible
secret has been detected.

Ignored local configuration, reports, model caches and Terraform state must not
be included in a publication archive. Use the version-controlled files rather
than uploading the complete working directory. Do not force-add ignored files.

**Existing Git history still contains identifiers from earlier versions.** History
was preserved as requested; cleaning current files does not anonymize old commits
or author metadata. Publishing this history exposes those earlier identifiers.

## Deployment boundaries

- Put the gateway behind HTTPS. Its default Docker port binds only to loopback;
  do not expose the coordinator directly or accept client-supplied proxy identity
  headers from the internet. Protect the trusted reverse proxy.
- Keep all three API keys separate and rotate them after suspected exposure.
  The agent key is shared across trusted workers: a holder can impersonate another
  worker. Per-agent credentials and per-user quotas are not implemented.
- Agents execute code supplied by the coordinator's auto-update channel. Trust
  that coordinator and its operators as software administrators on participating
  machines. TLS does not protect a machine against a compromised coordinator.
- Keep native llama.cpp RPC on loopback or an isolated trusted network. QUIC uses
  pinned peer certificates and session credentials; raw RPC is not an internet
  service. Models and native runtimes are part of the trusted software supply chain.
- Container jobs are administrator-only. Docker access is powerful, and resource
  restrictions are not a guarantee against hostile workloads or kernel exploits.
  Use dedicated disposable hosts for experiments with untrusted code.
- Dashboard chat history remains in browser local storage. Clear browser data on
  shared lab PCs. Admin-only telemetry and durable reports can include hostnames,
  addresses, user paths and backend diagnostics; treat them as private records.
- This review adds no student identity management, consent workflow, retention
  policy, or institutional compliance guarantee. Assess those before handling
  sensitive student data.

## Reporting a vulnerability

Do not post credentials, private reports or an exploit against a live deployment
in a public issue. Use the repository host's private vulnerability reporting when
available, or a private channel to the maintainer. Reproduce issues with synthetic
data and disposable credentials.
