# PeerFoundry naming and compatibility

Public documentation, dashboard, package metadata, OpenCode provider, generated
client commands, proxy example and new infrastructure defaults use **PeerFoundry**.
The default public prefix is `/peerfoundry/`; OpenCode reads
`PEERFOUNDRY_BASE_URL` and `PEERFOUNDRY_CONSUMER_KEY`.

The gateway also accepts `/swarm/` for saved client URLs and installed agents.
Client loaders accept the old `SWARM_BASE_URL` and `SWARM_CONSUMER_KEY` settings;
the launcher supplies both sets of placeholders. OpenCode setup migrates the old
`swarm-llm` provider while preserving saved profiles and unrelated providers.

Some legacy identifiers are deliberately stable: `X-Swarm-Agent-ID`,
`X-Swarm-Status`, QUIC ALPN/certificate names and probe bytes, existing agent
installation directories, startup launchers, identity files, caches and logs.
Changing these during a rolling agent update could break connections, duplicate
agents or trigger large downloads. Their remaining occurrences describe protocol
or persisted-state compatibility, rather than public product branding.

The renamed files are `nginx/peerfoundry.conf.example` and
`scripts/configure-peerfoundry-proxy.py`. Update local automation that refers to
the former filenames. Configure the upstream reverse proxy to forward
`/peerfoundry/` before deploying the new dashboard; accepting only the old prefix
upstream will not serve the dashboard's new asset URLs.

Existing ignored environment files and deployment-specific proxy routes are
preserved. No coordinator deployment, cloud apply, state migration, history rewrite
or repository-host rename is performed by these source changes. The local checkout
directory and Git remote are unchanged.

Terraform defaults and generated service names now use PeerFoundry. For an existing
deployment, inspect `terraform plan` and retain previous explicit names where
necessary: a changed physical resource name may cause replacement. Do not discard
or recreate state to perform a branding update.
