from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class IntegrationError(Exception):
    pass


def _run(argv: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    except OSError as exc:
        raise IntegrationError(f"cannot run {argv[0]}: {exc}") from exc
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise IntegrationError(f"{argv[0]} failed: {message}")
    return result.stdout.strip()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class JJRepository:
    """Read-only JJ/Git identity adapter plus one explicit bookmark move."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not (self.root / ".jj").is_dir():
            raise IntegrationError(f"not a JJ workspace: {self.root}")

    def jj(self, *args: str) -> str:
        return _run(["jj", "--ignore-working-copy", "-R", str(self.root), *args], cwd=self.root)

    def resolve(self, ref: str) -> str:
        value = self.jj("log", "--no-graph", "-r", ref, "-T", "commit_id")
        if not _HEX40.fullmatch(value):
            raise IntegrationError(f"ref is missing, conflicted, or ambiguous: {ref}")
        return value

    def git_root(self) -> Path:
        pointer = self.root / ".jj" / "repo"
        if pointer.is_file():
            store = (pointer.parent / pointer.read_text(encoding="utf-8").strip()).resolve()
            for candidate in (store.parent.parent, *store.parents):
                if (candidate / ".git").exists():
                    return candidate
            raise IntegrationError("JJ repository has no discoverable backing Git worktree")
        if (self.root / ".git").exists():
            return self.root
        raise IntegrationError("JJ repository has no discoverable backing Git worktree")

    def tree(self, commit: str) -> str:
        value = _run(["git", "-C", str(self.git_root()), "rev-parse", f"{commit}^{{tree}}"], cwd=self.root)
        if not _HEX40.fullmatch(value):
            raise IntegrationError(f"cannot resolve immutable Git tree for {commit}")
        return value

    def between(self, ancestor: str, descendant: str) -> list[str]:
        output = self.jj("log", "--no-graph", "-r", f"{ancestor}..{descendant}", "-T", "commit_id")
        if len(output) % 40:
            raise IntegrationError("JJ returned malformed commit identities for ancestry range")
        return [output[index:index + 40] for index in range(0, len(output), 40)]

    def conflict(self, commit: str) -> bool:
        value = self.jj("log", "--no-graph", "-r", commit, "-T", "conflict")
        if value not in {"true", "false"}:
            raise IntegrationError(f"cannot determine conflict state for {commit}")
        return value == "true"

    def change(self, commit: str) -> dict[str, Any]:
        description = self.jj("log", "--no-graph", "-r", commit, "-T", "description")
        files = self.jj("diff", "--from", f"{commit}-", "--to", commit, "--summary").splitlines()
        return {"commit": commit, "description": description, "files": files, "conflicted": self.conflict(commit)}

    def operation_id(self) -> str:
        value = self.jj("op", "log", "--no-graph", "-n", "1", "-T", "id")
        if not _HEX64.fullmatch(value):
            raise IntegrationError("could not identify the JJ operation created by promotion")
        return value

    def move_bookmark(self, name: str, commit: str) -> None:
        self.jj("bookmark", "move", name, "--to", commit)

    def op_exists(self, operation_id: str) -> bool:
        output = self.jj("op", "log", "--no-graph", "-T", "id ++ \"\\n\"")
        return operation_id in output.splitlines()


def _admission_blockers(document: Any, source: str, target: str, tree: str, commits: list[str], base_dir: Path) -> tuple[list[str], dict[str, Any]]:
    blockers: list[str] = []
    summary: dict[str, Any] = {"schema": None, "issues": [], "evidence": [], "tests": [], "reviewer": None}
    if not isinstance(document, dict):
        return ["admission record must be a JSON object"], summary
    summary["schema"] = document.get("schema")
    if document.get("schema") == "symphony-issue-eligibility.v1":
        check = document.get("last_check") or {}
        review = document.get("review") or {}
        staging = document.get("staging")
        if check.get("status") != "ready":
            blockers.append(f"staging eligibility is {check.get('status', 'unknown')}: {check.get('reason', 'no ready status')}")
        if not review.get("approved_at") or not review.get("candidate_sha") or not review.get("reviewer"):
            blockers.append("independent review decision is absent or incomplete")
        if not staging:
            blockers.append("staging admission receipt is absent")
        blockers.append("eligibility state is not an integration admission receipt")
        summary["issues"] = [document.get("issue")] if document.get("issue") else []
        summary["reviewer"] = review.get("reviewer")
        return blockers, summary

    if document.get("schema") != "prf-dev.integration-admission.v1":
        return ["unsupported admission record schema"], summary
    expected_fields = {"schema", "status", "source_commit", "target_commit", "reviewed_commits",
                       "review", "staging", "qualification", "issues", "evidence"}
    if set(document) != expected_fields:
        blockers.append("admission record fields do not match the v1 contract")

    def check_file_ref(item: Any, label: str) -> None:
        if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
            blockers.append(f"{label} evidence reference is missing")
            return
        digest = str(item.get("sha256", ""))
        if not _HEX64.fullmatch(digest):
            blockers.append(f"{label} evidence digest is malformed")
            return
        path = Path(item["ref"]).expanduser()
        if not path.is_absolute():
            path = base_dir / path
        try:
            if path.is_symlink() or not path.is_file() or _sha(path.read_bytes()) != digest:
                blockers.append(f"{label} evidence reference is missing or has a digest mismatch")
        except OSError:
            blockers.append(f"{label} evidence reference cannot be read")
    if document.get("status") != "admitted":
        blockers.append("candidate is not marked admitted")
    if document.get("source_commit") != source or document.get("target_commit") != target:
        blockers.append("admission record source/target commits do not match current bookmarks")
    review = document.get("review")
    if not isinstance(review, dict):
        blockers.append("review evidence fields do not match the v1 contract")
        review = {}
    elif set(review) != {"status", "candidate_commit", "reviewer", "ref", "sha256"}:
        blockers.append("review evidence fields do not match the v1 contract")
    if (review.get("status") != "approved" or review.get("candidate_commit") != source
            or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
            or not _HEX64.fullmatch(str(review.get("sha256", "")))):
        blockers.append("independent review evidence is missing, stale, or not bound to the exact candidate")
    check_file_ref(review, "review")
    reviewed = document.get("reviewed_commits")
    if not isinstance(reviewed, list) or reviewed != commits:
        blockers.append("review does not cover the exact ordered set of included commits")
    qualification = document.get("qualification")
    if not isinstance(qualification, dict):
        blockers.append("qualification evidence fields do not match the v1 contract")
        qualification = {}
    elif set(qualification) != {"status", "commit", "tree", "tests"}:
        blockers.append("qualification evidence fields do not match the v1 contract")
    if qualification.get("status") != "passed" or qualification.get("commit") != source or qualification.get("tree") != tree:
        blockers.append("integration qualification is missing or does not bind the exact staged tree")
    tests = qualification.get("tests")
    if (not isinstance(tests, list) or not tests
            or any(not isinstance(test, dict) or set(test) != {"command", "status", "tree"}
                   or not isinstance(test.get("command"), str) or not test.get("command")
                   or test.get("status") != "passed" or test.get("tree") != tree for test in tests)):
        blockers.append("passing validation evidence for the exact staged tree is missing")
    stage = document.get("staging")
    if not isinstance(stage, dict):
        blockers.append("staging evidence fields do not match the v1 contract")
        stage = {}
    elif set(stage) != {"status", "source_commit", "target_commit", "ref", "sha256"}:
        blockers.append("staging evidence fields do not match the v1 contract")
    if (stage.get("status") != "admitted" or stage.get("source_commit") != source
            or stage.get("target_commit") != target
            or not _HEX64.fullmatch(str(stage.get("sha256", "")))):
        blockers.append("staging admission evidence is missing or stale")
    check_file_ref(stage, "staging")
    issues = document.get("issues")
    if not isinstance(issues, list) or not issues or any(not isinstance(issue, str) or not issue.strip() for issue in issues):
        blockers.append("associated issue IDs are missing")
    evidence = document.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        blockers.append("content-addressed evidence references are missing")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict) or set(item) != {"ref", "sha256"}:
                blockers.append(f"evidence[{index}] fields do not match the v1 contract")
            check_file_ref(item, f"evidence[{index}]")
    summary.update({"issues": issues if isinstance(issues, list) else [],
                    "evidence": evidence if isinstance(evidence, list) else [],
                    "tests": tests if isinstance(tests, list) else [],
                    "reviewer": review.get("reviewer")})
    return blockers, summary


def preflight(repo: JJRepository, source_ref: str, target_ref: str, admission_path: str | Path) -> dict[str, Any]:
    blockers: list[str] = []
    admission_file = Path(admission_path).expanduser().resolve()
    try:
        admission_bytes = admission_file.read_bytes()
        admission = json.loads(admission_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        admission_bytes, admission = b"", None
        blockers.append(f"cannot read valid admission evidence: {type(exc).__name__}")
    try:
        source, target = repo.resolve(source_ref), repo.resolve(target_ref)
        source_tree, target_tree = repo.tree(source), repo.tree(target)
        commits = repo.between(target, source)
        reverse = repo.between(source, target)
        if repo.conflict(target):
            blockers.append(f"integration target contains conflicts: {target}")
        if reverse:
            blockers.append("source is not a fast-forward descendant of target")
        if not commits:
            blockers.append("source and target contain no promotable changes")
        changes = [repo.change(commit) for commit in commits]
        for change in changes:
            if change["conflicted"]:
                blockers.append(f"included commit contains conflicts: {change['commit']}")
        admission_reasons, summary = _admission_blockers(admission, source, target, source_tree, commits, admission_file.parent)
        blockers.extend(admission_reasons)
        report: dict[str, Any] = {
            "schema": "prf-dev.integration-preflight.v1",
            "status": "ready" if not blockers else "blocked",
            "repository": str(repo.root),
            "source": {"ref": source_ref, "commit": source, "tree": source_tree},
            "target": {"ref": target_ref, "commit": target, "tree": target_tree},
            "included_changes": changes,
            "admission_record": {"path": str(admission_file), "sha256": _sha(admission_bytes), **summary},
            "blockers": blockers,
        }
    except IntegrationError as exc:
        report = {"schema": "prf-dev.integration-preflight.v1", "status": "blocked",
                  "repository": str(repo.root), "source_ref": source_ref, "target_ref": target_ref,
                  "admission_record": {"path": str(admission_file), "sha256": _sha(admission_bytes)},
                  "included_changes": [], "blockers": blockers + [str(exc)]}
    digest = _sha(_json_bytes(report))
    report["report_sha256"] = digest
    return report


def _repo_lock(repo: JJRepository) -> Path:
    pointer = repo.root / ".jj" / "repo"
    if pointer.is_file():
        store = (pointer.parent / pointer.read_text(encoding="utf-8").strip()).resolve()
    else:
        store = pointer.resolve()
    return store.parent / f".{store.name}.integration-promotion.lock"


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_json(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    _fsync_directory(path.parent)


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _json_bytes(value)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise IntegrationError("cannot create promotion record: path already exists") from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def promote(repo: JJRepository, source_ref: str, target_ref: str, admission_path: str | Path,
            preflight_path: str | Path, expected_preflight_sha256: str, record_path: str | Path) -> dict[str, Any]:
    if not _HEX64.fullmatch(expected_preflight_sha256):
        raise IntegrationError("a full preflight SHA-256 is required")
    preflight_file = Path(preflight_path).expanduser().resolve()
    try:
        saved = json.loads(preflight_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationError("cannot read preflight report") from exc
    saved_digest = saved.pop("report_sha256", None)
    actual_digest = _sha(_json_bytes(saved))
    if saved_digest != expected_preflight_sha256 or actual_digest != expected_preflight_sha256:
        raise IntegrationError("preflight report digest does not match the independently supplied pin")
    if saved.get("status") != "ready" or saved.get("repository") != str(repo.root):
        raise IntegrationError("preflight is not ready for this repository")

    lock_path = _repo_lock(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntegrationError("another integration promotion holds the repository lock") from exc
        fresh = preflight(repo, source_ref, target_ref, admission_path)
        if fresh["status"] != "ready" or fresh["report_sha256"] != expected_preflight_sha256:
            raise IntegrationError("bookmarks or evidence changed since preflight; requalification required")
        record_file = Path(record_path).expanduser().resolve()
        if record_file == preflight_file:
            raise IntegrationError("promotion record must not overwrite the preflight report")
        try:
            record_file.relative_to(repo.root)
        except ValueError:
            pass
        else:
            raise IntegrationError("promotion record must be outside the checked-out repository")
        record = {"schema": "prf-dev.integration-promotion.v1", "status": "promotion-started",
                  "repository": str(repo.root), "preflight_sha256": expected_preflight_sha256,
                  "source": fresh["source"], "target_before": fresh["target"],
                  "included_changes": fresh["included_changes"], "admission_record": fresh["admission_record"],
                  "issues": fresh["admission_record"].get("issues", []), "tests": fresh["admission_record"].get("tests", [])}
        _write_json_exclusive(record_file, record)
        try:
            repo.move_bookmark(target_ref, fresh["source"]["commit"])
            operation_id = repo.operation_id()
            final_commit = repo.resolve(target_ref)
            final_tree = repo.tree(final_commit)
            if final_commit != fresh["source"]["commit"] or final_tree != fresh["source"]["tree"]:
                raise IntegrationError("post-move bookmark or tree differs from the qualified candidate")
            record.update({"status": "promoted", "result": {"ref": target_ref, "commit": final_commit, "tree": final_tree},
                           "jj_operation_id": operation_id})
            _replace_json(record_file, record)
            return record
        except Exception as exc:
            record.update({"status": "promotion-failed", "failure": f"{type(exc).__name__}: {exc}"})
            _replace_json(record_file, record)
            raise


def verify(repo: JJRepository, record_path: str | Path) -> dict[str, Any]:
    try:
        record = json.loads(Path(record_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrationError("cannot read promotion record") from exc
    if record.get("schema") != "prf-dev.integration-promotion.v1" or record.get("status") != "promoted":
        raise IntegrationError("promotion record is missing or not successful")
    if record.get("repository") != str(repo.root):
        raise IntegrationError("promotion record belongs to a different repository")
    result = record.get("result") or {}
    current = repo.resolve(result.get("ref", ""))
    result_commit = result.get("commit", "")
    if repo.tree(result_commit) != result.get("tree"):
        raise IntegrationError("recorded promotion tree identity is invalid")
    if current != result_commit and not repo.between(result_commit, current):
        raise IntegrationError("integration bookmark no longer descends from the recorded promotion result")
    included = record.get("included_changes")
    target_before = (record.get("target_before") or {}).get("commit", "")
    if not isinstance(included, list) or not included:
        raise IntegrationError("promotion record has no included-change lineage")
    recorded_commits = [item.get("commit") for item in included if isinstance(item, dict)]
    if len(recorded_commits) != len(included) or repo.between(target_before, result_commit) != recorded_commits:
        raise IntegrationError("promotion record does not match the resulting commit lineage")
    if not repo.op_exists(record.get("jj_operation_id", "")):
        raise IntegrationError("recorded JJ operation is absent from operation history")
    return {"schema": "prf-dev.integration-verification.v1", "status": "verified",
            "result": result, "current_integration_commit": current, "jj_operation_id": record["jj_operation_id"],
            "included_commits": [item["commit"] for item in included]}


def trigger(repo: JJRepository, source_ref: str, target_ref: str, review_path: str | Path,
            staging_path: str | Path, issue: str, reviewer: str, records_dir: str | Path) -> dict[str, Any]:
    """Auto-promote only a newly admitted, exactly reviewed fast-forward candidate."""
    records = Path(records_dir).expanduser().resolve()
    records.mkdir(parents=True, exist_ok=True)
    source = repo.resolve(source_ref)
    target = repo.resolve(target_ref)
    source_tree = repo.tree(source)
    target_tree = repo.tree(target)
    admission_path = records / f"admission-{source[:12]}.json"
    preflight_path = records / f"preflight-{source[:12]}.json"
    promotion_path = records / f"promotion-{source[:12]}.json"

    def blocked(*reasons: str) -> dict[str, Any]:
        return {"schema": "prf-dev.integration-trigger.v1", "status": "blocked",
                "source": {"ref": source_ref, "commit": source, "tree": source_tree},
                "target": {"ref": target_ref, "commit": target, "tree": target_tree},
                "blockers": list(reasons)}

    if source == target and promotion_path.is_file():
        try:
            verified = verify(repo, promotion_path)
        except IntegrationError as exc:
            return blocked(f"existing promotion record does not verify: {exc}")
        return {"schema": "prf-dev.integration-trigger.v1", "status": "already-promoted",
                "admission_record": str(admission_path), "promotion_record": str(promotion_path),
                "verification": verified}

    try:
        review_file = Path(review_path).expanduser().resolve()
        review_bytes = review_file.read_bytes()
        review = json.loads(review_bytes)
        stage_file = Path(staging_path).expanduser().resolve()
        stage_bytes = stage_file.read_bytes()
        stage = json.loads(stage_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        return blocked(f"cannot read valid review and staging evidence: {type(exc).__name__}")

    review_fields = {"schema", "issue", "verdict", "candidate_sha", "base_sha", "reviewed_tests", "finding"}
    if (not isinstance(review, dict) or set(review) != review_fields
            or review.get("schema") != "symphony-pilot-review-decision.v1"
            or review.get("issue") != issue or review.get("verdict") != "approved"):
        return blocked("review decision is not a valid approved record for the admitted issue")
    if not isinstance(stage, dict) or stage.get("schema") != "symphony-staging-receipt.v1" or stage.get("issue") != issue:
        return blocked("staging receipt is not a valid receipt for the admitted issue")
    if not isinstance(reviewer, str) or not reviewer.strip():
        return blocked("SYMPHONY_REVIEWER_ID is required to identify the independent reviewer")
    if stage.get("new_staging_sha") != source or stage.get("validated_tree") != source_tree:
        return blocked("staging receipt does not bind the current staging tip and its exact tree")
    if (stage.get("candidate_sha") != source or stage.get("base_sha") != target
            or stage.get("staging_bookmark") != source_ref):
        return blocked("staging receipt candidate, base, or bookmark does not match the promotion refs")
    if stage.get("old_staging_sha") != target:
        return blocked("staging admission did not start at the current integration tip")
    if review.get("candidate_sha") != source or review.get("base_sha") != target:
        return blocked("review decision does not bind the exact integration-to-staging candidate")
    if stage.get("integration_mode") != "fast-forward":
        return blocked("automatic integration requires a fast-forward staging admission")

    try:
        included = repo.between(target, source)
        reverse = repo.between(source, target)
    except IntegrationError as exc:
        return blocked(str(exc))
    stack = stage.get("candidate_commits")
    stack_ids = [item.get("sha") for item in stack] if isinstance(stack, list) and all(isinstance(item, dict) for item in stack) else []
    if reverse or not included:
        return blocked("staging tip is not a non-empty fast-forward descendant of integration")
    if stack_ids != included:
        return blocked("reviewed commits do not exactly cover the ordered integration-to-staging range")
    if (not isinstance(stage.get("tests"), list) or not stage["tests"]
            or any(not isinstance(command, str) or not command.strip() for command in stage["tests"])):
        return blocked("staging receipt has no recorded validation commands")

    evidence = []
    evidence_paths = [review_file, stage_file]
    log_value = stage.get("log")
    if isinstance(log_value, str) and log_value:
        evidence_paths.append(Path(log_value).expanduser().resolve())
    for path in evidence_paths:
        try:
            if path.is_symlink() or not path.is_file():
                return blocked(f"required evidence file is missing or unsafe: {path}")
            evidence.append({"ref": str(path), "sha256": _sha(path.read_bytes())})
        except OSError:
            return blocked(f"required evidence file cannot be read: {path}")

    admission = {
        "schema": "prf-dev.integration-admission.v1", "status": "admitted",
        "source_commit": source, "target_commit": target, "reviewed_commits": included,
        "review": {"status": "approved", "candidate_commit": source, "reviewer": reviewer.strip(),
                   "ref": str(review_file), "sha256": _sha(review_bytes)},
        "staging": {"status": "admitted", "source_commit": source, "target_commit": target,
                    "ref": str(stage_file), "sha256": _sha(stage_bytes)},
        "qualification": {"status": "passed", "commit": source, "tree": source_tree,
                          "tests": [{"command": str(command), "status": "passed", "tree": source_tree}
                                    for command in stage["tests"]]},
        "issues": [issue], "evidence": evidence,
    }
    admission_bytes = _json_bytes(admission)
    try:
        if admission_path.exists():
            if admission_path.read_bytes() != admission_bytes:
                return blocked("existing admission record differs; preserve and inspect it")
        else:
            _write_json_exclusive(admission_path, admission)
    except OSError as exc:
        return blocked(f"cannot persist admission record: {type(exc).__name__}")

    report = preflight(repo, source_ref, target_ref, admission_path)
    _replace_json(preflight_path, report)
    if report.get("status") != "ready":
        return {"schema": "prf-dev.integration-trigger.v1", "status": "blocked",
                "admission_record": str(admission_path), "preflight_report": str(preflight_path),
                "blockers": report.get("blockers", [])}
    try:
        promoted = promote(repo, source_ref, target_ref, admission_path, preflight_path,
                           report["report_sha256"], promotion_path)
        verified = verify(repo, promotion_path)
    except IntegrationError as exc:
        return {"schema": "prf-dev.integration-trigger.v1", "status": "blocked",
                "admission_record": str(admission_path), "preflight_report": str(preflight_path),
                "promotion_record": str(promotion_path), "blockers": [str(exc)]}
    return {"schema": "prf-dev.integration-trigger.v1", "status": "promoted",
            "admission_record": str(admission_path), "preflight_report": str(preflight_path),
            "promotion_record": str(promotion_path), "promotion": promoted, "verification": verified}
