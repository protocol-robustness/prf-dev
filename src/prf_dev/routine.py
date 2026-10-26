from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from prf_dev.integration import IntegrationError, JJRepository


class RoutineError(Exception):
    pass


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _policy(path: Path) -> tuple[dict, str]:
    value = _read(path)
    if (set(value) != {"schema", "policy_id", "principals"}
            or value.get("schema") != "prf-dev.routine-trust-policy.v1"
            or not isinstance(value.get("policy_id"), str) or not value["policy_id"].strip()
            or not isinstance(value.get("principals"), dict) or not value["principals"]):
        raise RoutineError("unsupported routine trust policy")
    for principal, entry in value["principals"].items():
        if (not isinstance(principal, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+", principal) or not isinstance(entry, dict)
                or set(entry) != {"public_key", "roles", "repositories", "status"}
                or not isinstance(entry.get("public_key"), str)
                or not entry["public_key"].startswith("ssh-ed25519 ")
                or not isinstance(entry.get("roles"), list) or not entry["roles"]
                or any(not isinstance(role, str) or role not in {"routine-coder", "routine-reviewer", "routine-stager"} for role in entry["roles"])
                or not isinstance(entry.get("repositories"), list) or not entry["repositories"]
                or any(not isinstance(repo, str) or not repo for repo in entry["repositories"])
                or entry.get("status") not in {"active", "revoked", "retired"}):
            raise RoutineError(f"malformed routine trust principal: {principal}")
    return value, _digest(value)


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise RoutineError(f"expected regular non-symlink file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutineError(f"cannot read JSON record {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RoutineError(f"expected JSON object: {path}")
    return value


def _run(argv: list[str], *, cwd: Path, input_data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(argv, cwd=cwd, input=input_data, capture_output=True, check=False)
    except OSError as exc:
        raise RoutineError(f"cannot run {argv[0]}: {exc}") from exc
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
        raise RoutineError(f"{argv[0]} failed: {message}")
    return result.stdout


def _signed(record: dict, *, role: str, principal: str, key: Path, output: Path) -> dict:
    payload = _canonical(record)
    with tempfile.TemporaryDirectory(prefix="prf-routine-sign-") as raw:
        payload_path = Path(raw) / "payload"
        payload_path.write_bytes(payload)
        sig_path = _run(["ssh-keygen", "-Y", "sign", "-f", str(key.expanduser()), "-n", role, str(payload_path)], cwd=Path(raw))
        del sig_path
        signature = payload_path.with_name("payload.sig").read_bytes()
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    signature_name = output.name + ".sig"
    signature_path = output.parent / signature_name
    if output.exists() or signature_path.exists():
        raise RoutineError(f"refusing to overwrite existing routine record or signature: {output}")
    signature_path.write_bytes(signature)
    signed = {**record, "signature": {"principal": principal, "role": role, "namespace": role,
                                       "payload_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                                       "file": signature_name,
                                       "signed_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_canonical(signed))
    return signed


def submission(repo: JJRepository, *, repository_id: str, issue: str, base_ref: str, candidate_ref: str,
               principal: str, key: Path, evidence: Path, trust_path: Path, output: Path) -> dict:
    base, candidate = repo.resolve(base_ref), repo.resolve(candidate_ref)
    reverse = repo.between(candidate, base)
    commits = repo.between(base, candidate)
    if reverse or not commits:
        raise RoutineError("candidate must be a non-empty descendant of the declared base")
    if any(repo.conflict(commit) for commit in commits):
        raise RoutineError("candidate contains a conflicted change")
    if evidence.is_symlink() or not evidence.is_file():
        raise RoutineError("submission evidence must be a regular file")
    _, trust_root = _policy(trust_path)
    record = {"schema": "prf-dev.routine-submission.v1", "repository_id": repository_id,
              "issue": issue, "base_commit": base, "candidate_commit": candidate,
              "candidate_tree": repo.tree(candidate), "included_commits": commits,
              "trust_policy_root": trust_root,
              "evidence_ref": evidence.name, "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
              "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    return _signed(record, role="routine-coder", principal=principal, key=key, output=output)


def review(submission_path: Path, *, verdict: str, principal: str, key: Path, evidence: Path,
           trust_path: Path, output: Path) -> dict:
    candidate = _read(submission_path)
    if candidate.get("schema") != "prf-dev.routine-submission.v1":
        raise RoutineError("unsupported submission schema")
    if verdict not in {"approved", "rejected"}:
        raise RoutineError("review verdict must be approved or rejected")
    if evidence.is_symlink() or not evidence.is_file():
        raise RoutineError("review evidence must be a regular file")
    _, trust_root = _policy(trust_path)
    if candidate.get("trust_policy_root") != trust_root:
        raise RoutineError("submission trust policy differs from review policy")
    record = {"schema": "prf-dev.routine-review.v1", "issue": candidate["issue"],
              "repository_id": candidate["repository_id"], "submission_digest": _digest(candidate),
              "trust_policy_root": trust_root,
              "candidate_commit": candidate["candidate_commit"], "candidate_tree": candidate["candidate_tree"],
              "verdict": verdict, "evidence_ref": evidence.name,
              "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
              "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    return _signed(record, role="routine-reviewer", principal=principal, key=key, output=output)


def staging(repo: JJRepository, submission_path: Path, review_path: Path, *, staging_base_ref: str,
            staged_ref: str, principal: str, key: Path, tests: list[str], trust_path: Path, output: Path) -> dict:
    submitted, reviewed = _read(submission_path), _read(review_path)
    if reviewed.get("verdict") != "approved" or reviewed.get("submission_digest") != _digest(submitted):
        raise RoutineError("staging requires approval bound to the exact submission")
    reviewer_signature = reviewed.get("signature")
    if not isinstance(reviewer_signature, dict) or not isinstance(reviewer_signature.get("principal"), str):
        raise RoutineError("staging requires a signed independent review")
    if submitted["signature"]["principal"] == reviewer_signature["principal"]:
        raise RoutineError("coder cannot review their own submission")
    if (reviewed.get("candidate_commit") != submitted.get("candidate_commit")
            or reviewed.get("candidate_tree") != submitted.get("candidate_tree")):
        raise RoutineError("review candidate identity differs from the signed submission")
    base, staged = repo.resolve(staging_base_ref), repo.resolve(staged_ref)
    included = repo.between(base, staged)
    if not included or repo.between(staged, base):
        raise RoutineError("staging result must be a non-empty descendant of its declared base")
    if any(repo.conflict(commit) for commit in included):
        raise RoutineError("staging result contains a conflicted change")
    source = submitted["candidate_commit"]
    source_tree = submitted["candidate_tree"]
    successor = next((commit for commit in included if repo.tree(commit) == source_tree), None)
    if not successor:
        raise RoutineError("staging does not include a same-tree successor for the reviewed candidate")
    if not tests or any(not item.strip() for item in tests):
        raise RoutineError("staging receipt requires at least one named passing qualification")
    _, trust_root = _policy(trust_path)
    if submitted.get("trust_policy_root") != trust_root or reviewed.get("trust_policy_root") != trust_root:
        raise RoutineError("submission/review trust policy differs from staging policy")
    record = {"schema": "prf-dev.routine-staging-receipt.v1", "issue": submitted["issue"],
              "repository_id": submitted["repository_id"], "submission_digest": _digest(submitted),
              "trust_policy_root": trust_root,
              "review_digest": _digest(reviewed), "source_commit": source,
              "source_tree": source_tree, "staged_successor_commit": successor,
              "staged_successor_tree": repo.tree(successor), "staging_base_commit": base,
              "staging_result_commit": staged, "staging_result_tree": repo.tree(staged),
              "included_commits": included, "tests": tests,
              "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    return _signed(record, role="routine-stager", principal=principal, key=key, output=output)


def _verify_signature(record: dict, path: Path, trust: dict, role: str) -> str:
    sig = record.get("signature")
    if not isinstance(sig, dict) or set(sig) != {"principal", "role", "namespace", "payload_digest", "file", "signed_at"}:
        raise RoutineError(f"missing or malformed {role} signature")
    principal = sig["principal"]
    key = trust.get("principals", {}).get(principal)
    if not key or key.get("status") != "active" or role not in key.get("roles", []):
        raise RoutineError(f"principal is not trusted for {role}: {principal}")
    if key.get("repositories") and record.get("repository_id") not in key["repositories"]:
        raise RoutineError(f"principal is outside trusted repository scope: {principal}")
    if role not in {"routine-coder", "routine-reviewer", "routine-stager"} or sig["role"] != role or sig["namespace"] != role:
        raise RoutineError("signature role or domain namespace mismatch")
    unsigned = {name: value for name, value in record.items() if name != "signature"}
    payload = _canonical(unsigned)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if digest != sig["payload_digest"]:
        raise RoutineError("signed payload digest mismatch")
    signature_path = path.parent / sig["file"]
    if signature_path.is_symlink() or not signature_path.is_file() or signature_path.parent.resolve() != path.parent.resolve():
        raise RoutineError("signature file missing or unsafe")
    public = key.get("public_key", "")
    if not public.startswith("ssh-ed25519 ") or "\n" in public or "\r" in public:
        raise RoutineError("unsupported trusted public key")
    with tempfile.TemporaryDirectory(prefix="prf-routine-verify-") as raw:
        temp = Path(raw)
        allowed = temp / "allowed_signers"
        allowed.write_text(f"{principal} {public}\n", encoding="utf-8")
        allowed.chmod(0o600)
        payload_file = temp / "payload"
        payload_file.write_bytes(payload)
        signature_copy = temp / "signature"
        signature_copy.write_bytes(signature_path.read_bytes())
        _run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", principal,
              "-n", role, "-s", str(signature_copy)], cwd=temp, input_data=payload)
    return principal


def _verify_evidence(record: dict, record_path: Path) -> None:
    name = record.get("evidence_ref")
    if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
        raise RoutineError("evidence reference must be a sibling filename")
    evidence = record_path.parent / name
    if evidence.is_symlink() or not evidence.is_file():
        raise RoutineError("signed evidence artifact is missing or unsafe")
    if hashlib.sha256(evidence.read_bytes()).hexdigest() != record.get("evidence_sha256"):
        raise RoutineError("signed evidence digest mismatch")


def verify(repo: JJRepository, submission_path: Path, review_path: Path, staging_path: Path, *, trust_path: Path,
           repository_id: str, issue: str, source_ref: str, target_ref: str) -> dict:
    trust, trust_root = _policy(trust_path)
    submission_record, review_record, staging_record = _read(submission_path), _read(review_path), _read(staging_path)
    for record, expected, schema, path in [(submission_record, "routine-coder", "prf-dev.routine-submission.v1", submission_path),
                                           (review_record, "routine-reviewer", "prf-dev.routine-review.v1", review_path),
                                           (staging_record, "routine-stager", "prf-dev.routine-staging-receipt.v1", staging_path)]:
        if record.get("schema") != schema:
            raise RoutineError("unsupported routine record schema")
        _verify_signature(record, path, trust, expected)
    _verify_evidence(submission_record, submission_path)
    _verify_evidence(review_record, review_path)
    if any(item.get("repository_id") != repository_id for item in (submission_record, review_record, staging_record)):
        raise RoutineError("record is outside the requested repository scope")
    if any(item.get("issue") != issue for item in (submission_record, review_record, staging_record)):
        raise RoutineError("record issue does not match requested issue")
    if any(item.get("trust_policy_root") != trust_root for item in (submission_record, review_record, staging_record)):
        raise RoutineError("routine records do not bind to the independently supplied trust policy")
    if review_record.get("submission_digest") != _digest(submission_record):
        raise RoutineError("review does not bind to the exact signed submission")
    if (review_record.get("candidate_commit") != submission_record.get("candidate_commit")
            or review_record.get("candidate_tree") != submission_record.get("candidate_tree")):
        raise RoutineError("review candidate identity differs from the signed submission")
    if review_record.get("verdict") != "approved":
        raise RoutineError("independent review did not approve candidate")
    if staging_record.get("submission_digest") != _digest(submission_record) or staging_record.get("review_digest") != _digest(review_record):
        raise RoutineError("staging receipt does not bind the verified submission and review")
    if (staging_record.get("source_commit") != submission_record.get("candidate_commit")
            or staging_record.get("source_tree") != submission_record.get("candidate_tree")):
        raise RoutineError("staging source identity differs from the signed candidate")
    principals = [item["signature"]["principal"] for item in (submission_record, review_record, staging_record)]
    if len(set(principals)) != 3:
        raise RoutineError("coder, reviewer, and staging signer must be distinct")
    coder, reviewer, stager = principals
    source, target = repo.resolve(source_ref), repo.resolve(target_ref)
    if source != submission_record.get("candidate_commit") or repo.tree(source) != submission_record.get("candidate_tree"):
        raise RoutineError("current candidate commit/tree differs from signed submission")
    if target != staging_record.get("staging_result_commit"):
        raise RoutineError("requested staging ref does not match signed staging result")
    if repo.tree(target) != staging_record.get("staging_result_tree"):
        raise RoutineError("current staging result tree differs from signed receipt")
    included = repo.between(staging_record.get("staging_base_commit", ""), target)
    if staging_record.get("staging_base_commit") != submission_record.get("base_commit"):
        raise RoutineError("staging receipt base differs from the signed submission base")
    if included != staging_record.get("included_commits"):
        raise RoutineError("staging lineage differs from signed receipt")
    successor = staging_record.get("staged_successor_commit")
    if successor not in included or repo.tree(successor) != submission_record["candidate_tree"]:
        raise RoutineError("staging result does not contain a same-tree successor for the reviewed candidate")
    source_commits = repo.between(submission_record.get("base_commit", ""), source)
    if source_commits != submission_record.get("included_commits"):
        raise RoutineError("candidate lineage differs from signed submission")
    return {"schema": "prf-dev.routine-verification.v1", "status": "verified",
            "repository_id": repository_id, "issue": issue, "candidate_commit": source,
            "candidate_tree": repo.tree(source), "staging_commit": target,
            "staged_successor_commit": successor, "coder": coder, "reviewer": reviewer, "stager": stager,
            "claim_boundary": "verifies credential signatures and current candidate inclusion; does not prove process identity or signer isolation"}
