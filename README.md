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

See [ownership](docs/ownership.md), [environment profiles](docs/environment.md), [workflows](docs/workflows.md), [staging-to-integration promotion](workflows/integration/README.md), [routine signing policy](docs/routine-signing.md), [repair evidence](docs/repairs.md), and the [JJ + Symphony engineering recommendations](docs/agent-engineering-jj.md). The latter is guidance informed by a dated inspection; it does not certify deployed host controls.

## Routine signed candidate pilot

The `prf-dev routine` commands create a candidate submission, independent review, and staging receipt using distinct OpenSSH Ed25519 principals. Records bind exact JJ commit/tree identities, issue and repository IDs, evidence digests, and a separately supplied routine trust-policy root. `routine verify` is offline and checks the signed candidate, reviewer, staging result, source-to-staged-successor tree mapping, and current JJ refs. Keep the trust policy and private keys outside the repository; use separate OS identities or a restricted signing service in production. This initial verifier does not authenticate Symphony process identity or prove the listed test commands ran. Existing Symphony lanes are not yet wired to request these signatures, and unsigned legacy staging records remain outside this pilot.

For a local pilot, prepare a JJ repo with `base`, `candidate`, and `staging` refs, three role-scoped public keys in a `prf-dev.routine-trust-policy.v1` JSON file, and evidence files outside the checkout:

```sh
prf-dev routine submit --repo /tmp/candidate --repository-id prf --issue PRO-123 \
  --base-ref base --candidate-ref candidate --signer coder-1 --key /keys/coder \
  --evidence /records/coder-evidence.txt --trust-policy /policy/routine-trust.json \
  --output /records/submission.json
prf-dev routine review --submission /records/submission.json --verdict approved \
  --signer reviewer-1 --key /keys/reviewer --evidence /records/review.txt \
  --trust-policy /policy/routine-trust.json --output /records/review.json
prf-dev routine stage --repo /tmp/candidate --submission /records/submission.json \
  --review /records/review.json --staging-base-ref base --staged-ref staging \
  --signer stager-1 --key /keys/stager --tests /records/passed-tests.json \
  --trust-policy /policy/routine-trust.json --output /records/staging.json
prf-dev routine verify --repo /tmp/candidate --repository-id prf --issue PRO-123 \
  --candidate-ref candidate --staging-ref staging --submission /records/submission.json \
  --review /records/review.json --staging-receipt /records/staging.json \
  --trust-policy /policy/routine-trust.json
```

The tests file is a JSON list of qualification command names; the signer asserts those checks passed. It is not execution evidence. The staging pilot accepts a same-tree successor (including a rewritten commit) only when the stager signs the explicit source-to-successor mapping and the successor is in the current staging ancestry. It does not move bookmarks or push.
