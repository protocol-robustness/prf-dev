# Staging-to-integration promotion

`prf-dev integration` is the narrow promotion boundary between an admitted staging candidate and the protected `integration` bookmark. It is separate from staging admission and publication. It never pushes `main`.

## Admission contract

Preflight consumes a `prf-dev.integration-admission.v1` JSON record (schema: `schemas/integration-admission.v1.json`) produced by the staging admission owner. It binds the exact source/target commits, ordered set of reviewed commits, reviewer decision, staging receipt, validation commit/tree, issue IDs, and evidence digests. Evidence paths are relative to the admission record unless absolute; symlinks and digest mismatches fail closed. The record is an interoperability input, not a signature format. Until PRO-106 supplies authenticated admission records, reviewers and operators must treat provenance as procedural and retain that limitation.

A legacy `symphony-issue-eligibility.v1` record is accepted only to report its blocker reasons. It can never qualify a promotion by itself. In particular, `diverged`, missing review, or missing staging receipt blocks the candidate.

## Commands

Run from any working directory, with an explicit PRF JJ workspace and admission record. Keep all reports outside the checkout.

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

The Symphony staging admission host invokes the automatic trigger after writing a successful staging receipt. It requires `SYMPHONY_REVIEWER_ID` and uses the exact decision and receipt paths supplied by the host. The trigger only proceeds when one approved review covers every ordered commit from current `integration` to the new `agent-c-staging` tip, the admission is a fast-forward, and the staging validations bind the exact candidate tree. It writes the admission and preflight records, promotes under the same repository lock, then verifies the result. A failed condition emits a blocked result and leaves `integration` unchanged; the staging admission remains recorded. Records live outside the repository in the configured staging integrations directory.

For diagnostics or a controlled replay, the host operation can be run explicitly:

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
