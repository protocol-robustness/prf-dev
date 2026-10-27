# PRF agent engineering recommendations — JJ + Symphony

**Status:** Recommendations informed by a read-only workspace audit on 2026-10-25; not an authoritative inventory of every live host or deployed launcher
**Date:** 2026-10-25
**Home:** `docs/agent-engineering-jj.md`
**Audience:** Interactive coding agents, Symphony coding/reviewer lanes, staging/integration operators, and agents modifying their workflows.

## 1. Operating model: JJ-native, not PR-shaped

PRF development uses **Jujutsu (`jj`)**, multiple persistent workspaces, Symphony coding and reviewer workflows, a reviewed staging lineage (for example, `agent-c-staging`), and a separate **integration** workspace from which an authorized operator advances and pushes `main` using `jj`. **Pull requests are not a prerequisite and must not be invented as the acceptance mechanism.**

An issue, a JJ change, a workspace, a staged candidate, an accepted release and a published Git commit are different things. Avoid treating them as interchangeable.

The intended path is:

`Linear issue / scoped task` → `coding JJ workspace` → `candidate and evidence` → `independent read-only review` → `JJ staging integration` → `integration qualification and authorization` → `jj push of main` → `post-push verification`.

This describes responsibilities, not necessarily seven separate agents or tools. The coding, staging, and integration workspaces inspected on 2026-10-25 were entries in one shared JJ repository; a separate integration workspace is not a separate repository or store. Reuse existing lanes and checks. Some low-risk work may be combined where policy allows.

### Observed implementation snapshot (2026-10-25)

These observations describe inspected files and JJ state, not proof that every host is running the inspected launcher version:

- JJ 0.45.1 was installed. `agent-c`, `agent-c-1`, `agent-c-2`, `agent-c-3`, `agent-c-staging`, and `integration` appeared in the same repository's workspace list and bookmark/operation history. The 0.45.1 CLI documentation says `--at-op` implies `--ignore-working-copy`; nonetheless, the initial command sequence using `jj --at-op=@ workspace list/status/log/bookmark list/op log` emitted object-write errors in this checkout. The cause was not established. Subsequent read-only JJ inspections succeeded with both options explicit. Neither option prevents a mutating command from having effects, so choose read-only subcommands and separately assess external effects.
- Symphony's host `symphony-stage-reviewed.py` gate binds an approval decision to a candidate SHA, imports that exact candidate, rejects JJ conflicts, validates the assembled staging workspace, and updates the configured staging bookmark. This is a staging admission gate; it does not establish authorization to publish `main`.
- Reviewer read-only status is procedural in the inspected workflow instructions, not established as a technical boundary. The inspected `symphony` launcher uses `bwrap`, but binds `$CODE` and `~/.config/jj` read-write for the child process. It explicitly passes `LINEAR_API_KEY`; no publisher credential was explicitly passed in the inspected launcher, and the inspected Symphony scripts did not implement `jj git push`. This does not establish the effective mounts, inner Codex sandbox grants, inherited descriptors, SSH-agent access, or credentials of a deployed reviewer process; inspect that running process configuration separately before claiming enforcement.
- The inspected Symphony tree had unsnapshotted modifications during this audit. Treat the observations above as source inspection, not as confirmation that those changes are deployed.

**North star:** independently qualified outcomes per unit of engineering effort, not PR count, lines of code, or agent runs.

## 2. Core principles

1. **Make instructions navigable, not enormous.** Keep `AGENTS.md` as a short map to trusted versioned domain documentation, repo-specific workflows, tests and policies. Keep operational state, raw logs, private material and machine-specific setup outside public repositories.
2. **Enforce a few consequential invariants.** Hard checks matter for canonical roots and hashing, settlement invariants, authority, custody, deploys, signatures, and publication. Routine style preferences should not become heavyweight gates.
3. **Agents can write and review, but not grant themselves authority.** A second agent's opinion is useful review, not independently trusted acceptance by itself. For consequential claims, bind verification to exact source, evidence and policy.
4. **Keep security boundaries external to prompts.** A prompt saying “do not push” is not protection if the coding agent has publisher credentials. Enforce writable paths, executable permissions, allowed commands, signing access and network/secret boundaries outside the agent's editable workspace.
5. **Optimize observed failure modes.** Add a lint, wrapper, check or lane when it prevents actual defects or materially reduces rework. Do not add a general-purpose harness merely because other teams have one.

## 3. JJ-specific rules for multiple workspaces

- **Discover before changing.** At entry, record current workspace and repo, source revision and intended target. For non-mutating initial inspection prefer read-only subcommands such as `jj --ignore-working-copy --at-op=@ workspace list`, `status`, `log` and `op log` (adjust to installed JJ version). JJ 0.45.1 documents `--at-op` as implying `--ignore-working-copy`; passing both can make the intent explicit. An `--at-op` view can be stale relative to unsnapshotted disk changes. Neither flag makes a mutating subcommand read-only or prevents external side effects. Check the installed JJ version before automating CLI syntax.
- **Do not assume a workspace is an independent repo.** JJ workspaces typically share the underlying repository, bookmarks and operation history. Changes and rewrites can affect other workspaces even when working-copy directories differ.
- **Do not rewrite another agent's active working-copy change.** Avoid cross-workspace operations against the active `@` of another agent. Coordinate when an integration/rebase would rewrite another lane's candidate.
- **Keep staging as one integrated lineage; submit candidates through the staging queue.** A coding workspace may lag staging while work is in progress. Submit ready candidates through JJQ; do not move `agent-c-staging` directly from an issue workspace or leave parallel candidate tips attached to the shared staging bookmark. Configure the queue trunk as `agent-c-staging` with landing strategy `merge`. Concurrent `jjq push` commands only enqueue work; the queue integrates each candidate against the then-current trunk, checks the combined result, and advances the trunk only after it passes. The queue can check ahead, but landings must remain serialized. Use `jjq push --allow-stack` only for an explicitly reviewed multi-change stack; otherwise submit one change. If a candidate conflicts or fails, keep it off the shared bookmark, resolve it against current staging, rerun checks, and resubmit.

  After a queue item lands, confirm `agent-c-staging` points to the integrated result and that the candidate is its ancestor. In the canonical staging workspace, run `jj new <staging-bookmark> -m "Continue staging after sequence <n>"`, then check `jj log -r '@ | <staging-bookmark>'` and `jj status` from that workspace. The expected state is a clean worktree with `@` an empty child of the bookmark. Do not use `jj new <staging-bookmark> @-` as a substitute for merging the current WIP tip: `jj new` uses exactly the supplied parents, and `@-` omits `@`. Keep one canonical staging workspace; treat temporary candidate/validation workspaces separately and preserve any unique evidence or work before retiring them.
- **Handle stale workspaces explicitly.** A rewrite from another workspace can make a working copy stale. Diagnose with `jj status` and the operation/evolution history before considering `jj workspace update-stale`; that command can create a recovery change in some cases.
- **Beware automatic snapshots, including on observational commands.** Normal `jj status` and `jj op log` may snapshot and/or reconcile operations; use documented non-snapshotting options with read-only subcommands for an initial view. JJ snapshots worktree changes as revisions, and can automatically track previously untracked files. Keep build outputs, evidence dumps, credentials, logs and machine-local files out of tracked paths and in appropriate ignore/state directories.
- **Avoid repo-wide recovery commands casually.** `jj undo`, `jj op restore`, abandon, workspace removal, bookmark changes, and broad rebase/squash operations can impact work outside the visible workspace. Inspect the operation/evolution history (prefer `jj --at-op=@ op log` for a non-mutating view), identify the exact operation or revision, preserve forensic evidence, and obtain operator approval before repository-wide restoration.
- **Treat mutable change IDs differently from immutable commit identities.** JJ change IDs aid continuity across rewrites, but a rewrite changes the underlying commit ID; capture the final commit/tree identity for qualification. Check whether an existing review, signature, or approval still applies after a rewrite.
- **Preserve useful change lineage by default.** Do not force all staging changes into a single squashed revision. `jj squash`/rebase are valid for a coherent logical change, but broad flattening obscures attribution and prior accepted work. If history is intentionally rewritten, retain source→result mapping and re-check the final content.
- **Use repo-scoped access.** Allow agents to access their JJ repository and narrowly required `~/.config/jj` settings. Do not expose all of `~/.config`, publication credentials or signing keys merely to fix a JJ config issue. Read-only reviewer workspaces should not have write privileges to candidate/staging repositories.

## 4. Responsibilities in the current pipeline

### Coding lane — produce

- Start from an eligible, appropriately scoped issue and its declared repository/workspace. Respect unresolved dependencies even if the issue has moved to In Progress.
- Operate only in the assigned sandbox/workspace. Produce a candidate, a concise change summary, reproducible commands, the exact source identifiers and clear limitations.
- Keep tests and evidence with the claim they support; distinguish fixture/local-execution/proof/testnet/public-chain evidence.
- Do not move staging/`main` bookmarks, invoke publisher credentials, publish to production or mark your own result independently accepted.

### Reviewer lane — qualify

- Review the *exact* candidate/revision set and the evidence, using a separate context. Treat reviewer read-only access as procedural unless the deployed process's effective filesystem, sandbox, credential, and agent access have been inspected and technically demonstrated.
- Check scope, dependency state, affected authority and custody boundaries, suspicious file additions, conflicts, tests, negative tests and claimed provenance. Critical checks should use independently controlled fixtures or reproducible validation, not only the author's test narratives.
- Return PASS, REWORK or BLOCKED, with a precise evidence pointer. Reviewer approval is a bounded qualification result, **not** publication authority.
- Do not mutate author workspaces or staging while reviewing. If corrections are needed, return them to the coding lane.

### Staging integrator — compose

- Accept only reviewed candidates for the correct repository. Review the exact changes introduced against the current staging base, not just whether an earlier standalone candidate passed.
- Merge/rebase JJ revisions using the established staging flow; keep useful provenance. Resolve conflicts explicitly, record decisions and rerun affected tests after content or ancestry changes.
- Verify that the eventual staged tree contains the intended candidate and does not silently omit previously accepted changes. Never infer this solely from a green linear status or a JJ change ID.
- A completed issue may be Done when **staging acceptance** is the documented issue outcome; distinguish that from remote `main` publication. Keep stages clear without creating unnecessary Linear states.

### Integration publisher — authorize and publish

- **Only the authorized integration lane/operator moves and pushes the `main` bookmark.** It need not open a PR. A workflow wrapper may implement this, but enforcement must not depend solely on editable instructions.
- Pin the exact integrated commit and tree plus the observed remote `main` baseline. Recheck these immediately before publication; a preflight PASS expires if either changes.
- Use `jj git push`'s remote-position safety checks and `--dry-run` where useful. A policy preflight supplements JJ by checking PRF qualification and authorization evidence; it should not reimplement JJ's bookmark safety logic. Never override a mismatched remote ancestry or unresolved conflicts just to make a push succeed.
- Check unresolved conflicts, candidate-tree identity, integration tests/backstop, relevant repo-specific qualification results, risk-based approvals and pinned cross-repo dependencies against the **final assembled content**.
- Capture a compact publication record: source/reviewed change IDs and commit IDs, final commit/tree identity, baseline remote main, validation results/versions, approving authority, actual pushed target, and observed remote tip afterward.
- Publish explicitly with the configured `jj git push` bookmark/remote target only after authorization. **Post-push readback matters.** A local pre-push test is not a server-enforced gate; if the remote host cannot enforce eligibility, recognize that limitation and confine the push credential to the integration publisher.
- If remote `main` advanced or a relevant revision was rewritten since qualification, stop and rerun the affected checks. JJ push safety failures and unresolved ancestry must be resolved explicitly, never bypassed to force publication.

## 5. Risk-proportionate acceptance

| Work type | Suggested minimum | Extra control |
|---|---|---|
| Low: docs, examples, non-behavioral formatting | Scope check, existing relevant tests/docs check, ordinary reviewer or existing staging gate | No separate human approval unless policy requires it |
| Medium: implementation, non-critical tests, tooling | Independent agent review, reproducible checks, staging integration validation | Regression-focused checks for changed boundaries |
| High: contracts, custody/payouts, canonical identity, proof admission, governance, auth, sandbox, signer/publisher, deployment | Independent targeted validation, negative/adversarial tests, final assembled-tree verification, explicit authorized decision | Protect keys/credentials, strong evidence/provenance; human or already-authorized policy authority before publish |

Classification is about **impact and authority**, not code size. A one-line canonical hashing change may be high risk; a large documentation edit may be low risk.

## 6. Signatures and provenance — avoid false assurances

- Do not equate a commit author, workspace name, JJ change ID, or self-declared agent name with authenticated approval.
- JJ supports signing commits, but signing a rewritten commit may require a new signature. Signing the published commit and having an authorized reviewer approve the change are different assertions.
- Prefer a **minimal accepted-candidate record** binding `{repository, source identifiers, final commit/tree, scope, verification evidence, policy, approver identity/key, decision}`. Preserve authorization across merges only if the accepted subject remains the same; otherwise requalify/reapprove as policy dictates.
- Store private signing keys outside writable agent environments. Use an explicit authorized publisher/reviewer key roster; protect and version approval policy. Do not implement a new PKI or general attestations service solely to solve a documentation problem.
- For JJ repair and history rewriting, keep a mapping from source revisions to published result, the chosen repair operation and independent verification. Do not claim successful repair implies historical authorship or approvals were recovered.

## 7. A minimal near-term implementation sequence

**Do now (small changes):**

1. Publish this policy or its distilled version in `prf-dev`; link it from existing agent entry points without duplicating instructions in every workflow.
2. Inventory the *actual* coding, reviewer, staging and integration paths, including JJ config mounts and publisher credential access. Record where each boundary is truly enforced versus documented only.
3. Build a small **standalone, read-only integration preflight** and post-push verifier. The preflight binds the exact integrated commit/tree, remote `main` baseline, conflict state, and existing qualification/authorization evidence without mutating the shared JJ repository; it rechecks those bindings immediately before publication and fails closed if the remote baseline or candidate changed. JJ itself remains responsible for its push safety checks. The post-push verifier independently reads remote `main`, compares commit and tree with the intended result, and writes a durable verification record. Until the actual publication path enforces the preflight, it is advisory only.

An operator override may address missing policy evidence only when the operator is already authorized and records a durable reason. It cannot waive mismatched remote ancestry, a changed preflight binding, or unresolved conflicts. Exercise stale preflight, divergent history, missing authorization, and mismatched post-push result cases with local fixtures. Keep this standalone: do not add a Symphony publisher or modify the existing staging gate as part of this proposal.

**Next if justified by gaps:** isolate `main` push credentials to integration, add a minimal signed approval record for high-risk publishing/repair, ensure staging has change lineage mapping, and measure post-staging defects and rework.

**Do not introduce by default:** mandatory Git PRs, a new global orchestrator, a second canonical authority, per-issue permanent workspaces, a forced single squash, extra reviewers for every low-risk change, or broad writable config mounts.

## 8. Acceptance checks for anyone applying this policy

- Existing JJ workflow still works from named agent workspaces through staging and integration to direct `main` push; no PR requirement or unauthorized push was introduced.
- Repo-wide recovery and rewriting are explicitly guarded, including stale-workspace behavior.
- Any claim that reviewers cannot write, reach publisher credentials, or impersonate publishers is backed by inspection or tests of the deployed access boundary, not prompts alone.
- Final source and results of verification are attributable, with honest qualification boundaries.
- If a check is described as **enforced**, provide the actual technical enforcement point and a test. Otherwise mark it as a recommended/manual control.
- Added complexity is proportionate and preserves current small-team operational simplicity.

## References

- Jujutsu, *Working copy* (including shared workspaces and stale working copy): https://docs.jj-vcs.dev/latest/working-copy/
- Jujutsu, *Bookmarks*: https://docs.jj-vcs.dev/latest/bookmarks/
- Jujutsu, *Operation log*: https://docs.jj-vcs.dev/latest/operation-log/
- Jujutsu, *Configuration* (commit signatures, signing behavior): https://docs.jj-vcs.dev/latest/config/
- Jujutsu, *CLI reference* (`jj git push`, workspaces): https://docs.jj-vcs.dev/latest/cli-reference/
- Jujutsu 0.45.1, *CLI reference* (`--at-op`, `--ignore-working-copy`, `jj git push --dry-run` and push safety checks): https://docs.jj-vcs.dev/v0.45.1/cli-reference/

*This document recommends boundaries; it does not assert that all current PRF/Symphony workspaces, wrappers or publication gates already implement them.*
