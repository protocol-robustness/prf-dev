# Staging-to-integration promotion

`prf-dev integration` is the narrow promotion boundary between an admitted staging candidate and the protected `integration` bookmark. It is separate from staging admission and publication. It never pushes `main`.

## Admission contract

Preflight consumes a `prf-dev.integration-admission.v1` JSON record (schema: `schemas/integration-admission.v1.json`) produced by the staging admission owner. It binds the exact source/target commits, ordered set of reviewed commits, reviewer decision, staging receipt, validation commit/tree, issue IDs, and evidence digests. Evidence paths are relative to the admission record unless absolute; symlinks and digest mismatches fail closed. The record is an interoperability input, not a signature format. Until PRO-106 supplies authenticated admission records, reviewers and operators must treat provenance as procedural and retain that limitation.

Preflight reports use `prf-dev.integration-preflight.v2`; v2 adds the exact workspace-lineage inventory. Promotion rejects older preflight reports and requires a fresh v2 digest.

A legacy `symphony-issue-eligibility.v1` record is accepted only to report its blocker reasons. It can never qualify a promotion by itself. In particular, `diverged`, missing review, or missing staging receipt blocks the candidate.

## Commands

Run from any working directory, with an explicit PRF JJ workspace and admission record. Keep all reports outside the checkout.

Before staging admission, and again after integration promotion, inventory committed work in
registered agent workspaces. The audit checks `agent-*` and `symphony-*-coding-*` workspaces
against both protected refs. A commit present in staging but not integration is reported as
staged and awaiting promotion; a commit absent from staging is outstanding agent work and must
be included in a reviewed staging candidate or explicitly retained as work in progress. Do not
silently discard an `attention` report. Retain the report with the staging/integration handoff.
An `attention` exit status means at least one exact commit ID is missing from one or both refs;
it does not decide whether that work is ready to land. Integration preflight embeds the same
inventory in its report, and the preflight digest binds that inventory so promote rechecks it.
Review the embedded inventory before promotion; `preflight.status: ready` does not mean unrelated
workspace commits have been admitted.

```sh
prf-dev integration audit-workspaces \
  --repo /path/to/prf-jj-workspace \
  --staging-ref agent-c-staging \
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
  --source-ref agent-c-staging \
  --target-ref integration \
  --admission-record /path/to/integration-admission.json \
  --output /path/to/preflight.json
```

Preflight uses JJ with `--ignore-working-copy`, reports exact commit and Git tree IDs, verifies forward ancestry and an empty reverse range, enumerates each included commit and its files, rejects conflicted commits, and checks admission/review/test evidence. It does not move bookmarks or run code. A report with `status: blocked` exits nonzero.

Promotion verifies the report's independently supplied SHA-256, reacquires the current refs and admission evidence under a repository-scoped lock, and rejects any changed input before moving the target bookmark directly to the immutable source commit. It writes an exclusive promotion record before changing the ref and completes that record with the resulting commit/tree and JJ operation ID. It does not squash, rebase, clean workspaces, or push.

After `prf-dev integration verify` succeeds, refresh the integration workspace working copy from the promoted bookmark. From the integration workspace, run `jj new agent-c-staging`; this leaves `@` as a new empty change whose parent is the promoted commit. Confirm with `jj status` and `jj log -r "@ | @-" --no-graph`. The promotion bookmark and verification record remain pinned to the promoted commit; the new empty working-copy change is only the workspace starting point for subsequent integration work. If the integration workspace has uncommitted changes, preserve or resolve them before switching it.

```sh
prf-dev integration promote \
  --repo /path/to/prf-jj-workspace \
  --preflight-report /path/to/preflight.json \
  --preflight-sha256 <independently-retained-report-digest> \
  --admission-record /path/to/integration-admission.json \
  --record /path/to/promotion-record.json

prf-dev integration verify \
  --repo /path/to/prf-jj-workspace \
  --record /path/to/promotion-record.json
```

## Relationship to staging admission

PRF Symphony now separates the quick per-candidate screen from full staging qualification. The reviewer `after_run` hook validates the exact approved candidate, runs `bb backstop:fast` against a temporary merge with current `agent-c-staging`, and records it under the host's pending staging queue. A pending candidate does not move `agent-c-staging`, create an accepted staging receipt, mark its Linear issue Done, or unlock strict dependencies.

Inspect pending candidates on the Symphony staging host:

```sh
python3 "$SYMPHONY_ELIXIR_ROOT/symphony-stage-reviewed.py" --list-pending prf
```

Run the following on the Symphony host from a shell with the same environment as its worker (repo config, workspace root, and Linear credentials). When at least two compatible candidates are ready, or a single candidate needs to proceed immediately, run full qualification for the selected set:

```sh
python3 "$SYMPHONY_ELIXIR_ROOT/symphony-stage-reviewed.py" --qualify-pending prf PRO-123 PRO-124
```

Select one or more issue identifiers explicitly; the command does not batch the entire queue by default. It rejects candidates that modify the same paths, have stale or changed review decisions, fail dependency eligibility, conflict with the current staging head, or cannot pass full validation. The path-overlap check is only a mechanical filter: the operator must also exclude issues with semantically coupled changes across different files. Conflict and validation failures preserve pending records without advancing `agent-c-staging`. A successful batch runs the configured full validation once against the exact combined tree, advances only `agent-c-staging`, writes a batch record under the host staging-integrations directory plus a full staging receipt for each issue, then marks those issues Done. The batch record lists the exact candidate set and combined tree; each issue receipt binds its candidate and tests to that same validated tree. Retain both forms of evidence for the integration handoff. Strict dependents remain blocked until their upstream receipt exists. The bookmark update precedes receipt and Linear writes; if either post-promotion step fails, preserve the log and reconcile the written staging result before retrying.

Keep `agent-c-staging` as one integrated lineage. In the Symphony qualification path, each selected candidate is combined with the current staging tip in an isolated workspace, the exact combined tree is fully validated, and the bookmark advances only to that candidate or merge result; conflicts and validation failures leave the shared bookmark unchanged. For repositories using JJQ, configure the queue trunk as `agent-c-staging` and its landing strategy as `merge`. `jjq push` submits work to the queue; it does not itself land or advance the shared bookmark. The queue may receive candidates concurrently, but it must integrate and land them serially against the then-current trunk after checks pass. Keep conflicting candidates off the shared bookmark until resolved and rechecked against current staging. Use `--allow-stack` only when the reviewed candidate is intentionally a multi-change stack. Never create parallel tips by directly moving `agent-c-staging` from issue workspaces.

After staging qualification, integration remains a separate operator-controlled action from the integration workspace. The established operation is to create a JJ change from `agent-c-staging` there (for example, `jj new agent-c-staging`) and follow the current integration qualification and publication process. The expensive full staging suite need not be rerun if the exact qualified tree is promoted unchanged; if integration changes the tree, qualify that resulting tree before promotion.

`prf-dev integration trigger` is still an experimental one-shot fast-forward command, not the general batch integration workflow. It expects one issue receipt whose candidate is exactly the staging tip and whose admission is a fast-forward from the current integration tip. Batched Symphony receipts identify individual issue candidates and use batch-merge mode, so the trigger will block on them. Do not use it for batched staging until its admission contract is extended and qualified against disposable JJ repositories. The trigger does not implement the established `jj new agent-c-staging` operation and does not push `main`.

## Trigger limits and verification status

The trigger currently promotes by moving the target bookmark directly to the source commit. This is a fast-forward; it does not create a merge commit, run `jj new` in the `integration` workspace, or push `main`. It requires the staging receipt's old staging commit to equal the current integration commit, the receipt and review to bind the exact current staging tip, and the receipt to describe a fast-forward admission. If staging has accumulated other work since integration last moved, or staging admission used merge mode, the trigger blocks. It does not yet implement the general accumulated-staging workflow described above.

`tests/test_integration.py` covers the trigger's happy path, idempotence, and an unreviewed-commit blocker using a fake repository. I found no test that runs the trigger against a disposable real JJ repository. Treat it as unqualified for production use until that real-JJ path, failure cases, and the intended staging-to-integration history are exercised.

For a controlled invocation, supply the exact evidence and paths explicitly:

```sh
prf-dev integration trigger \
  --repo /path/to/prf-jj-workspace \
  --source-ref agent-c-staging \
  --target-ref integration \
  --review-decision /path/to/reviewer/ISSUE/review-decision.json \
  --staging-receipt /path/to/staging-receipt.json \
  --issue PRO-94 \
  --reviewer reviewer-identity \
  --records-dir /path/to/staging-integrations/integration
```

The trigger does not push or move any other bookmark. An already promoted candidate is accepted only when its existing promotion record verifies.

## Limits

The repository-scoped lock coordinates `prf-dev` promotions sharing the same JJ store; manual bookmark changes do not participate in that lock, so the immediate pre-move ref check and post-move identity check remain necessary. The recorded operation ID proves a JJ operation is present in local history, not who authorized it. This gate does not provide signatures, reviewer identity authentication, publication approval, or remote `main` verification.
