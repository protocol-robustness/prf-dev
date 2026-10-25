# prf-dev

`prf-dev` makes PRF development workflows reproducible, inspectable, and independently verifiable across repositories. It is a portable entry point for developers and reviewers. It consumes component-owned interfaces and policies; it is not a protocol authority, deployment system, or agent orchestrator.

## Quick start

Requires Python 3.11+, Git, Jujutsu, Clojure, and `ssh-keygen` for core verification. The PRF framework checkout supplies the canonical evidence hash implementation. Docker or Podman is needed only for the isolated reproduction example.

```sh
python3 -m pip install .
prf-dev init --workspace-root ~/Code
# Edit the generated XDG config to add repository checkout paths.
prf-dev doctor --profile core
prf-dev snapshot --profile core --strict
prf-dev reproduce repair-example-001 --isolated
prf-dev verify examples/repair-example-001 --offline --trust-policy /path/to/trust-policy.json
```

`doctor` is read-only. `init` writes private local configuration and XDG state/cache directories. `snapshot` records exact Git commit/tree identities, cleanliness, JJ observations, component-owned lock input digests, profile and policy roots. Strict mode rejects missing or dirty checkouts. `reproduce` requires explicit isolation and a digest-pinned image, with networking disabled. `verify` is offline and does not execute report-supplied commands.

A snapshot root identifies declared inputs; it does not prove that they were trustworthy or executed. Reproduction establishes that the declared procedure ran again and matched the fixture output. Repair verification separately checks signatures, scope, authorization, execution, review and evidence digests against an independently supplied trust policy. A signature authenticates a credential, not process identity or historical truth. Findings are claim scoped.

## Ownership boundaries

- Symphony owns agent orchestration and runtime workflow.
- protocol-deployments owns deployment configuration, execution, and deployment report verification.
- PRF framework owns protocol semantics and canonical evidence hashing.
- prf-dev owns portable cross-repository snapshots, adapters, qualification entry points, and examples.

Repair evidence schemas are interoperability views aligned with PRO-90. Routine agent submission and staging remain owned by PRO-106/Symphony. This repository does not define new production authority, signer identity, or protected publication rules.

## Local data

Configuration, state and cache follow XDG locations: `$XDG_CONFIG_HOME/prf-dev`, `$XDG_STATE_HOME/prf-dev`, and `$XDG_CACHE_HOME/prf-dev` (or their standard home defaults). Keep credentials and private evidence outside the source checkout.

## Public release

`PUBLICATION.allowlist` defines the exact source tree. `tools/export-publication` creates a clean curated copy and rejects unexpected or sensitive-looking content; `tools/verify-publication` checks a prepared export. No operational reports, local run logs, workspace paths, or historical repository state are included. The export tool does not publish or push.

See [ownership](docs/ownership.md), [environment profiles](docs/environment.md), [workflows](docs/workflows.md), [staging-to-integration promotion](workflows/integration/README.md), [repair evidence](docs/repairs.md), and the [JJ + Symphony engineering recommendations](docs/agent-engineering-jj.md). The latter is guidance informed by a dated inspection; it does not certify deployed host controls.
