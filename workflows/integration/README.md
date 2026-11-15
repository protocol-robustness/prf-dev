# Staging-to-integration promotion

`prf-dev integration` is the narrow promotion boundary between an admitted staging candidate and the protected `integration` bookmark. It is separate from staging admission and publication. It never pushes `main`.

## Admission contract

Preflight consumes a `prf-dev.integration-admission.v2` JSON record (`schemas/integration-admission.v2.json`). It binds exact source/target commits and trees, the exact ordered included range, and digest-pinned review, staging, and validation evidence. The review artifact must conform to `schemas/integration-review.v1.json`; the aggregate staging artifact must conform to `schemas/staging-admission.v1.json`. Both artifacts must independently name the same source, target, trees, issue set, and complete ordered range as the admission record. Per-test receipts bind output evidence to the exact source tree; command names alone are insufficient. Evidence paths are relative to the admission record unless absolute; symlinks and digest mismatches fail closed. These records are interoperability evidence, not signatures. Review provenance remains procedural until authenticated admission is available.

Preflight reports use `prf-dev.integration-preflight.v2`; v2 adds the exact workspace-lineage inventory. Promotion rejects older preflight reports and requires a fresh v2 digest.

A legacy `symphony-issue-eligibility.v1` record is accepted only to report its blocker reasons. It can never qualify a promotion by itself. In particular, `diverged`, missing review, or missing staging receipt blocks the candidate.

## Commands

Run from any working directory, with an explicit PRF JJ workspace and admission record. Keep all reports outside the checkout.

Before staging admission, and again after integration promotion, inventory committed work in
registered agent workspaces. The audit checks `agent-*` and `symphony-*-coding-*` workspaces
against interactive `agent-c-staging`, automated `symphony-staging`, and `integration`. A commit present in staging but not integration is reported as
staged and awaiting promotion; a commit absent from staging is outstanding agent work and must
be included in a reviewed staging candidate or explicitly retained as work in progress. Do not
silently discard an `attention` report. Retain the report with the staging/integration handoff.
An `attention` exit status means at least one exact commit ID is missing from both staging refs and integration;
it does not decide whether that work is ready to land. Integration preflight embeds the same
inventory in its report, and the preflight digest binds that inventory so promote rechecks it.
The audit uses both protected staging bookmarks and `integration`, even when
preflight's source is an immutable candidate commit ID.
Review the embedded inventory before promotion; `preflight.status: ready` does not mean unrelated
workspace commits have been admitted.

```sh
prf-dev integration audit-workspaces \
  --repo /path/to/prf-jj-workspace \
  --staging-refs agent-c-staging symphony-staging \
  --integration-ref integration \
  --output /path/to/workspace-lineage-audit.json
```

This is an ancestry audit of non-empty commits reachable from currently registered matching
workspaces. It reports commit IDs, trees, descriptions, conflicts, and membership in each ref.
It does not scan uncommitted files, deleted/unregistered workspaces, or prove that a feature is
behaviorally enabled. A legitimate rebase can replace a commit ID; review such cases as rewritten
lineage instead of treating the old ID as a byte-for-byte landed commit.

```sh
prf-dev integration preflight \
  --repo /path/to/prf-jj-workspace \
  --source-ref <assembled-candidate-commit> \
  --target-ref integration \
  --staging-refs agent-c-staging symphony-staging \
  --admission-record /path/to/integration-admission.json \
  --output /path/to/preflight.json
```

Preflight uses JJ with `--ignore-working-copy`, reports exact commit and Git tree IDs, explicitly checks that integration is a member of `ancestors(staging)`, enumerates the ordered included range, inspects each commit against every parent, rejects conflicted commits, and checks admission/review/test evidence. It does not move bookmarks or run code. A report with `status: blocked` exits nonzero.

Promotion verifies the report's independently supplied SHA-256, reacquires the current refs and admission evidence under a repository-scoped lock, and rejects any changed input before a conditional `jj bookmark move --from <expected-target> --to <source>`. It repeats the workspace-lineage audit with the same `--staging-refs` supplied to preflight; the CLI defaults to both `agent-c-staging` and `symphony-staging`. It writes an exclusive in-progress promotion record before changing the ref and completes that record only after verifying the resulting commit/tree and JJ operation ID. Qualification test entries require digest-bound execution evidence; command names alone are not evidence. The trigger blocks unless its staging receipt supplies those execution receipts. It does not squash, rebase, clean workspaces, or push.

After `prf-dev integration verify` succeeds, refresh the integration workspace working copy from the promoted bookmark. Protected bookmark moves do not automatically move every workspace's working-copy commit: after a queue landing, the `agent-c-staging` workspace may still be at its previous `@`; after Symphony staging qualification, the `symphony-staging` workspace may still be at its previous `@`; after integration promotion, the `integration` workspace may still be at its previous `@`. This does not mean the bookmark move failed.

In the corresponding workspace, inspect `jj status` and compare `@` with the bookmark. If the working copy is clean and stale, run `jj edit agent-c-staging` in the staging workspace or `jj edit integration` in the integration workspace. `jj edit` changes that workspace's checkout only; it does not move the bookmark or push anything. If `jj status` shows changes, stop and preserve or resolve them before switching. Recheck with `jj status` and `jj log -r "@ | agent-c-staging"` or `jj log -r "@ | integration"`; the working-copy and bookmark labels should identify the same commit. From the integration workspace, then run `jj new agent-c-staging` if a new empty working-copy change is needed for subsequent integration work. The promotion bookmark and verification record remain pinned to the promoted commit.

```sh
prf-dev integration promote \
  --repo /path/to/prf-jj-workspace \
  --preflight-report /path/to/preflight.json \
  --preflight-sha256 <independently-retained-report-digest> \
  --admission-record /path/to/integration-admission.json \
  --staging-refs agent-c-staging symphony-staging \
  --record /path/to/promotion-record.json

prf-dev integration verify \
  --repo /path/to/prf-jj-workspace \
  --record /path/to/promotion-record.json
```

## Retaining promotion evidence

Retain the raw promotion record and supporting evidence byte-for-byte in an access-controlled durable archive. Do not commit raw host paths, workspace inventories, or operator data to a public repository. For a public repository, commit a path-sanitized summary under `evidence/integration-promotions/YYYY-MM-DD/<promoted-commit-prefix>/` with source and target identities, the exact included commit IDs, JJ operation ID, qualification status, and original artifact digests. Mark the summary as non-authoritative: it is not a promotion gate record and cannot replace the raw evidence during verification. Record failed or incomplete attempts separately; never replace them with a later successful receipt.

## Separate interactive and Symphony staging streams

Interactive/JJQ work owns `agent-c-staging`. Symphony coding and batch qualification own the
separate `symphony-staging` bookmark. Both refs share their established source history, but their
writers are isolated: neither workflow may advance the other's bookmark. The workspace audit treats
a commit as staged when it is reachable from either ref. Do not point JJQ at `symphony-staging` or
configure Symphony to write `agent-c-staging`.

Integration must assemble the selected tips with the current `integration` target in an isolated
candidate workspace, for example `jj new integration agent-c-staging symphony-staging` in that
temporary workspace. The assembled candidate must descend from `integration` and include the
selected changes from both streams. Review and validate the exact assembled tree and ordered range;
green per-lane staging checks do not prove the cross-lane combination is compatible. Aggregate review
and staging admission artifacts must identify that assembled candidate, target, issue set, and
digest-bound validation evidence. Current Symphony issue decisions and per-batch staging receipts do
not automatically produce the aggregate integration artifacts. Do not promote until that evidence
has been prepared and preflight is ready.

## Relationship to staging admission

PRF Symphony separates the quick per-candidate screen from full staging qualification. The reviewer `after_run` hook validates the exact approved candidate, runs `bb backstop:fast` against a temporary merge with current `symphony-staging`, and records it under the host's pending staging queue. A pending candidate does not move `symphony-staging`, create an accepted staging receipt, mark its Linear issue Done, or unlock strict dependencies.

Inspect pending candidates on the Symphony staging host:

```sh
python3 "$SYMPHONY_ELIXIR_ROOT/symphony-stage-reviewed.py" --list-pending prf
```

Run the following on the Symphony host from a shell with the same environment as its worker (repo config, workspace root, and Linear credentials). When at least two compatible candidates are ready, or a single candidate needs to proceed immediately, run full qualification for the selected set:

```sh
python3 "$SYMPHONY_ELIXIR_ROOT/symphony-stage-reviewed.py" --qualify-pending prf PRO-123 PRO-124
```

Select one or more issue identifiers explicitly; the command does not batch the entire queue by default. It rejects candidates that modify the same paths, have stale or changed review decisions, fail dependency eligibility, conflict with the current Symphony staging head, or cannot pass full validation. The path-overlap check is only a mechanical filter: the operator must also exclude issues with semantically coupled changes across different files. Conflict and validation failures preserve pending records without advancing `symphony-staging`. A successful batch runs the configured full validation once against the exact combined tree, advances only `symphony-staging`, writes a batch record under the host staging-integrations directory plus a full staging receipt for each issue, then marks those issues Done. The batch record lists the exact candidate set and combined tree; each issue receipt binds its candidate and tests to that same validated tree. Retain both forms of evidence for the integration handoff. Strict dependents remain blocked until their upstream receipt exists. The bookmark update precedes receipt and Linear writes; if either post-staging step fails, preserve the log and reconcile the written result before retrying.

Keep each staging ref as one integrated lineage. In the Symphony qualification path, candidates combine with current `symphony-staging` in an isolated workspace, and only that bookmark advances after full validation. Interactive JJQ work keeps `agent-c-staging` as its trunk with `merge` landing. Each writer serializes its own landings. Neither workflow may update the other's bookmark.

After both streams are staged, an operator selects and assembles the `agent-c-staging` and `symphony-staging` tips with current `integration` in an isolated candidate workspace; do not move `integration` while composing or reviewing. The candidate must descend from `integration` and include the selected changes from both streams. Run full validation against the exact assembled tree and ordered range; green per-lane staging checks do not prove the cross-lane combination is compatible. Prepare an aggregate `prf-dev.staging-admission.v1` record with digest-bound execution evidence, then route a distinct Linear integration-review issue to Symphony. Its host hook combines that qualification with the independent review, runs preflight, conditionally moves local `integration`, verifies the promotion receipt, and marks the integration-review issue Done. Missing, stale, failed, or changed evidence blocks the move. Publishing `main` remains separate and authorized independently.

`prf-dev integration trigger` remains a narrow one-issue, one-shot fast-forward command, not the general batch integration workflow. Its review input must be an independently prepared `prf-dev.integration-review.v1` record and its staging input must be an independently prepared `prf-dev.staging-admission.v1` record. Both must name exactly the requested issue and bind the same source/target commits, trees, and complete ordered range; the staging record also carries digest-bound execution receipts. The trigger only wraps and hashes these supplied records in the admission record. It never creates or upgrades review or staging approval documents. Legacy per-issue decision and staging receipt files are insufficient and block. Accumulated or multi-issue staging requires a separately prepared aggregate review/admission through the v2 contract. The trigger does not implement the established `jj new agent-c-staging` operation and does not push `main`.

## Trigger limits and verification status

The trigger, when supplied acceptable exact-range evidence, promotes by conditionally moving the target bookmark to the source commit. It does not create a merge commit, run `jj new` in the `integration` workspace, or push `main`. Accumulated staging still requires independent review and admission of the combined candidate; separate issue approvals are not combined by the trigger.

`tests/test_integration.py` retains fake-repository failure injection and exercises parent-aware inspection and promotion in disposable Git-backed JJ repositories, including two-parent resolution, stale bookmarks/evidence, conditional move failure, lock contention, and operation receipts. These tests do not exclude independent direct JJ writers; deployment still requires the single-writer policy below.

For a controlled invocation, supply the exact evidence and paths explicitly:

```sh
prf-dev integration trigger \
  --repo /path/to/prf-jj-workspace \
  --source-ref <assembled-candidate-commit> \
  --target-ref integration \
  --review-decision /path/to/reviewer/ISSUE/review-decision.json \
  --staging-receipt /path/to/staging-receipt.json \
  --issue PRO-94 \
  --reviewer reviewer-identity \
  --records-dir /path/to/staging-integrations/integration
```

The trigger does not push or move any other bookmark. An already promoted candidate is accepted only when its existing promotion record verifies.

## Limits

The repository-scoped lock coordinates `prf-dev` promotions sharing the same JJ store; all authorized integration promotion owners must route through this gate, and staging updates must be serialized by their existing Symphony/jjq owner. Operators must drain/hold that staging writer while preparing a promotion. Direct JJ bookmark commands do not participate in either lock. The conditional target move prevents overwriting a stale integration target, but an independent writer can still change staging between checks or bypass the gate; this remains an operational single-writer limitation. The recorded operation ID proves a JJ operation is present in local history, not who authorized it. This gate does not provide signatures, reviewer identity authentication, publication approval, or remote `main` verification.
