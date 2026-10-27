from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re

from prf_dev.integration import IntegrationError, JJRepository, preflight as integration_preflight, promote as integration_promote, verify as integration_verify, trigger as integration_trigger
from prf_dev.routine import RoutineError, submission as routine_submission, review as routine_review, staging as routine_staging, verify as routine_verify
import shutil
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[2]
_DATA_CANDIDATES = (ROOT, Path(sys.prefix) / "share" / "prf-dev")
DATA_ROOT = next((candidate for candidate in _DATA_CANDIDATES if (candidate / "schemas").is_dir()), ROOT)
SCHEMA_DIR = DATA_ROOT / "schemas"
PROFILE_DIR = DATA_ROOT / "profiles"
POLICY_DIR = DATA_ROOT / "policies"
EXAMPLE_DIR = DATA_ROOT / "examples"
SCHEMA = "workspace-snapshot.v1"
HASH_INTENT = ":evidence-record"
IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[a-f0-9]{64}$")


class PrfDevError(Exception):
    pass


def config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "prf-dev" / "local.toml"


def state_root() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "prf-dev"


def cache_root() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "prf-dev"


def load_config(required: bool = True) -> dict:
    path = config_path()
    if not path.exists():
        if required:
            raise PrfDevError(f"local config is missing; run `prf-dev init` first ({path})")
        return {}
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PrfDevError(f"cannot read local config {path}: {exc}") from exc


def run(argv: list[str], *, cwd: Path | None = None, timeout: int = 30, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, input=input_text, text=True, capture_output=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PrfDevError(f"could not run {argv[0]}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise PrfDevError(f"{argv[0]} failed: {detail}")
    return result.stdout.strip()


def canonical_hash(value: object, config: dict) -> str:
    """Delegate canonical hashing to PRF's registered evidence-record intent."""
    framework = config.get("repositories", {}).get("protocol-robustness-framework")
    if not framework:
        raise PrfDevError("local config must set repositories.protocol-robustness-framework for PRF canonical hashing")
    framework = Path(framework).expanduser().resolve()
    if not (framework / "deps.edn").is_file() or not (framework / "src/resolver_sim/hash/canonical.clj").is_file():
        raise PrfDevError(f"configured PRF framework checkout is incomplete: {framework}")
    expression = (
        "(require '[clojure.data.json :as json] '[resolver-sim.hash.canonical :as hc]) "
        "(let [v (json/read-str (slurp *in*) :key-fn keyword)] "
        "(println (hc/hash-with-intent {:hash/intent :evidence-record} v)))"
    )
    result = run(["clojure", "-M", "-e", expression], cwd=framework, timeout=180,
                 input_text=json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise PrfDevError("PRF canonical hasher returned a malformed digest")
    return f"sha256:{result}"


def _profile(name: str) -> dict:
    path = PROFILE_DIR / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrfDevError(f"invalid profile {path}: {exc}") from exc


def _profile_chain(name: str) -> list[dict]:
    chain = []
    seen = set()
    while name:
        if name in seen:
            raise PrfDevError(f"profile inheritance cycle at {name}")
        seen.add(name)
        profile = _profile(name)
        chain.append(profile)
        name = profile.get("extends")
    return chain


def cmd_doctor(args: argparse.Namespace) -> int:
    chain = _profile_chain(args.profile)
    profile = chain[0]
    checks = []
    tools = {}
    contracts = {}
    optional_contracts = {}
    for item in chain:
        tools.update({tool["executable"]: tool["required"] for tool in item["tools"]})
        contracts.update(item.get("repository_contracts", {}))
        optional_contracts.update(item.get("optional_repository_contracts", {}))
    for executable, required in sorted(tools.items()):
        found = shutil.which(executable)
        checks.append({"tool": executable, "required": required,
                       "found": bool(found), "path": found or None})
    config = load_config(required=False)
    for repo_id, contract in contracts.items():
        repo = config.get("repositories", {}).get(repo_id)
        root = Path(repo).expanduser() if repo else None
        present = bool(root and root.is_dir())
        checks.append({"repository": repo_id, "required": bool(contract), "lock_inputs": contract, "found": present,
                       "path": str(root) if present else None})
    for repo_id, contract in optional_contracts.items():
        repo = config.get("repositories", {}).get(repo_id)
        root = Path(repo).expanduser() if repo else None
        present = bool(root and root.is_dir())
        checks.append({"repository": repo_id, "required": False, "lock_inputs": contract,
                       "found": present, "path": str(root) if present else None})
    failures = [c for c in checks if c.get("required") and not c.get("found")]
    report = {"profile": args.profile, "suitable": not failures, "checks": checks,
              "claim_boundary": "environment suitability only; this does not authenticate future executions"}
    print(json.dumps(report, indent=2 if args.json else None, sort_keys=True))
    return 1 if failures else 0


def cmd_init(args: argparse.Namespace) -> int:
    target = config_path()
    if target.exists():
        raise PrfDevError(f"refusing to overwrite existing local config: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_root().mkdir(mode=0o700, parents=True, exist_ok=True)
    cache_root().mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = str(Path(args.workspace_root).expanduser().resolve()) if args.workspace_root else str((Path.home() / "src").resolve())
    content = f'''# Local paths only. Verification and integration policy is versioned in prf-dev.
workspace_root = {json.dumps(workspace)}
trust_policy = ""

[repositories]
# Add absolute checkout paths as needed, for example:
# protocol-robustness-framework = "{workspace}/protocol-robustness-framework"
# protocol-deployments = "{workspace}/protocol-deployments"
# symphony = "{workspace}/symphony"
'''
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    print(target)
    return 0


def _git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args], timeout=30)


def cmd_snapshot(args: argparse.Namespace) -> int:
    config = load_config()
    repositories = config.get("repositories", {})
    catalog_path = DATA_ROOT / "repos.yaml"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    profile_chain = _profile_chain(args.profile)
    profile = profile_chain[0]
    profile_contracts = {}
    for profile_part in profile_chain:
        profile_contracts.update(profile_part.get("repository_contracts", {}))
        profile_contracts.update(profile_part.get("optional_repository_contracts", {}))
    if not repositories:
        raise PrfDevError(f"no repository checkouts configured in {config_path()}")
    records = []
    for repo_id, location in sorted(repositories.items()):
        path = Path(location).expanduser().resolve()
        if not path.is_dir():
            if args.strict:
                raise PrfDevError(f"configured checkout is missing: {repo_id} ({path})")
            records.append({"repository_id": repo_id, "available": False})
            continue
        revision = _git(path, "rev-parse", "HEAD")
        tree = _git(path, "rev-parse", "HEAD^{tree}")
        dirty = bool(_git(path, "status", "--porcelain=v1", "--untracked-files=all"))
        if args.strict and dirty:
            raise PrfDevError(f"strict snapshot rejects dirty checkout: {repo_id}")
        record = {"repository_id": repo_id, "available": True, "git_commit": revision,
                  "git_tree": tree, "working_tree_clean": not dirty}
        if repo_id not in catalog["repositories"]:
            raise PrfDevError(f"configured repository is absent from the public catalog: {repo_id}")
        record["identity"] = catalog["repositories"][repo_id]
        for rel in profile_contracts.get(repo_id, []):
            lexical_source = path / rel
            source = lexical_source.resolve()
            if path not in source.parents or lexical_source.is_symlink() or not source.is_file():
                if args.strict:
                    raise PrfDevError(f"component lock input missing or unsafe: {repo_id}/{rel}")
                record.setdefault("component_lock_inputs", {})[rel] = None
            else:
                record.setdefault("component_lock_inputs", {})[rel] = hashlib.sha256(source.read_bytes()).hexdigest()
        if (path / ".jj").exists() and shutil.which("jj"):
            try:
                record["jj_change_id"] = run(["jj", "--ignore-working-copy", "--no-pager", "-R", str(path),
                                                "log", "--no-graph", "-r", "@", "-T", "change_id"], timeout=30)
                record["jj_commit_id"] = run(["jj", "--ignore-working-copy", "--no-pager", "-R", str(path),
                                                "log", "--no-graph", "-r", "@", "-T", "commit_id"], timeout=30)
                record["jj_operation_id"] = run(["jj", "--ignore-working-copy", "--no-pager", "-R", str(path),
                                                   "op", "log", "--limit", "1", "-T", "id"], timeout=30)
            except PrfDevError as exc:
                record["jj_observation"] = f"unavailable: {exc}"
        records.append(record)
    profile = _profile(args.profile)
    policy_hashes = {}
    for file in sorted(POLICY_DIR.glob("*.json")):
        data = json.loads(file.read_text(encoding="utf-8"))
        policy_hashes[file.name] = canonical_hash(data, config)
    snapshot = {
        "schema": SCHEMA,
        "created_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "profile": {"id": args.profile, "sha256": hashlib.sha256((PROFILE_DIR / f"{args.profile}.json").read_bytes()).hexdigest()},
        "toolchain_catalog_sha256": hashlib.sha256((PROFILE_DIR / "toolchains.lock").read_bytes()).hexdigest(),
        "repository_catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        "repositories": records,
        "toolchain_sources": profile_contracts,
        "policy_roots": policy_hashes,
        "trust_policy_root": canonical_hash(_load_json(Path(config["trust_policy"]).expanduser()), config) if config.get("trust_policy") else None,
        "procedure": {"id": "prf-dev.snapshot", "version": "0.1.0", "strict": bool(args.strict)},
        "claim_boundary": "declared input identity only; this root does not attest to execution or trustworthiness",
    }
    snapshot["snapshot_root"] = canonical_hash(snapshot, config)
    output = Path(args.output).expanduser() if args.output else state_root() / "snapshots" / f"{snapshot['snapshot_root'].split(':')[1][:16]}.json"
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    print(output)
    return 0


def _safe_file_tree(root: Path) -> tuple[str, list[dict]]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PrfDevError(f"reproduction outputs may not contain symlinks: {path}")
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            files.append({"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"sha256:{digest}", files


def cmd_reproduce(args: argparse.Namespace) -> int:
    if not args.isolated:
        raise PrfDevError("reproduction requires explicit --isolated")
    recipe_path = EXAMPLE_DIR / args.case / "reproduction.json"
    try:
        recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrfDevError(f"cannot load reproduction recipe: {exc}") from exc
    image = recipe.get("image", "")
    if not IMAGE_RE.fullmatch(image):
        raise PrfDevError("recipe image must be pinned by sha256 digest")
    runtime = shutil.which("docker") or shutil.which("podman")
    if not runtime:
        raise PrfDevError("isolated reproduction requires Docker or Podman")
    source = recipe_path.parent.resolve()
    command = recipe.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
        raise PrfDevError("recipe command must be a non-empty argv array")
    with tempfile.TemporaryDirectory(prefix="prf-dev-reproduce-") as temp:
        output = Path(temp) / "output"
        output.mkdir(mode=0o700)
        args_run = [runtime, "run", "--rm", "--network=none", "--read-only", "--user",
                    f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--pids-limit=96", "--memory=512m", "--cpus=1",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m", "--mount",
                    f"type=bind,src={source},dst=/input,readonly", "--mount",
                    f"type=bind,src={output},dst=/output", "--workdir=/input", image, *command]
        run(args_run, timeout=int(recipe.get("timeout_seconds", 120)))
        root, files = _safe_file_tree(output)
        expected = recipe.get("expected_output_root")
        status = "verified" if root == expected else "failed"
        result = {"schema": "reproduction-result.v1", "case": args.case, "status": status,
                  "image": image, "output_root": root, "expected_output_root": expected,
                  "files": files, "claim_boundary": "execution repeated in an isolated, network-disabled container"}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if status == "verified" else 1


def _verify_ssh_signature(*, digest: str, principal: str, signature: Path, namespace: str,
                          public_key: str, tempdir: Path) -> None:
    if signature.is_symlink() or not signature.is_file():
        raise PrfDevError(f"signature missing or unsafe: {signature.name}")
    allowed = tempdir / "allowed_signers"
    allowed.write_text(f"{principal} {public_key}\n", encoding="utf-8")
    allowed.chmod(0o600)
    run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", principal,
         "-n", namespace, "-s", str(signature)], cwd=signature.parent,
        timeout=30, input_text=digest)


def _load_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise PrfDevError(f"expected regular, non-symlink file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrfDevError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PrfDevError(f"expected JSON object in {path}")
    return value


def _record_digest(record: dict, config: dict) -> str:
    unsigned = {key: value for key, value in record.items() if key != "signature"}
    return canonical_hash(unsigned, config)


def _check_signature(record: dict, bundle: Path, trust: dict, config: dict, role: str,
                     used_principals: set[str], now: dt.datetime, scope: dict) -> None:
    sig = record.get("signature")
    if not isinstance(sig, dict):
        raise PrfDevError(f"missing required {role} signature")
    principal = sig.get("principal")
    key = trust.get("principals", {}).get(principal)
    if not key or role not in key.get("roles", []) or key.get("status") != "active":
        raise PrfDevError(f"principal is not active and authorized for role {role}: {principal}")
    public_key = key.get("public_key", "")
    if not public_key.startswith("ssh-ed25519 ") or "\n" in public_key or "\r" in public_key:
        raise PrfDevError(f"unsupported or malformed public key for principal {principal}")
    if principal in used_principals:
        raise PrfDevError(f"signer role collision: {principal}")
    for field in ("valid_from", "valid_until"):
        bound = key.get(field)
        if bound:
            instant = dt.datetime.fromisoformat(bound.replace("Z", "+00:00"))
            if (field == "valid_from" and instant > now) or (field == "valid_until" and instant <= now):
                raise PrfDevError(f"principal key is not valid now: {principal}")
    for field, values in (("repositories", [scope.get("repository_id")]),
                          ("refs", [scope.get("source_ref"), scope.get("target_ref")]),
                          ("operations", scope.get("operations", []))):
        allowed = key.get(field, [])
        if any(value and value not in allowed for value in values):
            raise PrfDevError(f"principal is outside its trusted {field} scope: {principal}")
    if sig.get("namespace") != role:
        raise PrfDevError(f"signature namespace does not match role {role}")
    signed_at = sig.get("signed_at")
    if signed_at:
        signed_instant = dt.datetime.fromisoformat(signed_at.replace("Z", "+00:00"))
        if signed_instant > now:
            raise PrfDevError(f"{role} signature is dated in the future")
    expires = record.get("expires_at")
    if expires:
        expiry = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if expiry <= now:
            raise PrfDevError(f"{role} record has expired")
    digest = _record_digest(record, config)
    expected = sig.get("payload_digest")
    if expected != digest:
        raise PrfDevError(f"{role} payload digest mismatch")
    signature_source = bundle / sig.get("file", "")
    if signature_source.is_symlink():
        raise PrfDevError("signature file may not be a symlink")
    signature = signature_source.resolve()
    if bundle.resolve() not in signature.parents:
        raise PrfDevError("signature path escapes evidence bundle")
    with tempfile.TemporaryDirectory(prefix="prf-dev-verify-") as raw:
        _verify_ssh_signature(digest=digest, principal=principal, signature=signature,
                              namespace=role, public_key=public_key, tempdir=Path(raw))
    used_principals.add(principal)


def _schema_type_matches(value: object, kind: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "null": value is None,
    }.get(kind, False)


def _validate_schema_value(value: object, schema: dict, label: str, root_schema: dict) -> None:
    ref = schema.get("$ref")
    if ref:
        file_name, _, fragment = ref.partition("#")
        ref_schema = _load_json(SCHEMA_DIR / file_name) if file_name else root_schema
        for component in filter(None, fragment.split("/")):
            ref_schema = ref_schema[component.replace("~1", "/").replace("~0", "~")]
        _validate_schema_value(value, ref_schema, label, ref_schema)
        return
    expected = schema.get("type")
    if isinstance(expected, str) and not _schema_type_matches(value, expected):
        raise PrfDevError(f"{label} must be {expected}")
    if "const" in schema and value != schema["const"]:
        raise PrfDevError(f"{label} does not match its required constant")
    if "enum" in schema and value not in schema["enum"]:
        raise PrfDevError(f"{label} has an unsupported value")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise PrfDevError(f"{label} is too short")
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            raise PrfDevError(f"{label} has an invalid format")
        if schema.get("format") == "date-time":
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise PrfDevError(f"{label} is not an ISO-8601 date-time") from exc
            if parsed.tzinfo is None:
                raise PrfDevError(f"{label} must include a timezone")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise PrfDevError(f"{label} has too few items")
        item_schema = schema.get("items", {})
        for index, item in enumerate(value):
            _validate_schema_value(item, item_schema, f"{label}[{index}]", root_schema)
    if isinstance(value, dict):
        missing = [field for field in schema.get("required", []) if field not in value]
        if missing:
            raise PrfDevError(f"{label} missing required fields: {', '.join(missing)}")
        props = schema.get("properties", {})
        extras = sorted(set(value) - set(props))
        additional = schema.get("additionalProperties", True)
        if additional is False and extras:
            raise PrfDevError(f"{label} has unsupported fields: {', '.join(extras)}")
        for key, item in value.items():
            if key in props:
                child_schema = props[key]
            elif isinstance(additional, dict):
                child_schema = additional
            else:
                continue
            _validate_schema_value(item, child_schema, f"{label}.{key}", root_schema)


def _check_schema(record: dict, name: str) -> None:
    schema = _load_json(SCHEMA_DIR / name)
    _validate_schema_value(record, schema, name, schema)


def cmd_verify(args: argparse.Namespace) -> int:
    if not args.offline:
        raise PrfDevError("verification is offline-only in v0; pass --offline")
    lexical_target = Path(args.artifact).expanduser()
    if lexical_target.is_symlink():
        raise PrfDevError("verification target may not be a symlink")
    target = lexical_target.resolve()
    config = load_config()
    if target.is_file():
        snapshot = _load_json(target)
        _check_schema(snapshot, "workspace-snapshot.v1.json")
        root = snapshot.pop("snapshot_root", None)
        if not root or canonical_hash(snapshot, config) != root:
            raise PrfDevError("workspace snapshot commitment mismatch")
        result = {"schema": "snapshot-verification.v1", "status": "verified", "artifact": target.name, "claims": [
            {"claim": "declared-input-identity", "status": "verified"},
            {"claim": "execution-occurred", "status": "unknown"},
            {"claim": "authorization", "status": "unknown"}],
            "claim_boundary": "a valid snapshot root identifies declared input data only"}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    bundle = target
    if not bundle.is_dir():
        raise PrfDevError("artifact must be a snapshot file or repair evidence directory")
    trust_path = Path(args.trust_policy).expanduser() if args.trust_policy else Path(config.get("trust_policy", "")).expanduser()
    if not str(trust_path) or not trust_path.is_file():
        raise PrfDevError("an independently supplied --trust-policy is required to verify repair evidence")
    trust_path = trust_path.resolve()
    if target == trust_path or (target.is_dir() and target in trust_path.parents):
        raise PrfDevError("trust policy must be supplied independently from the evidence bundle")
    trust = _load_json(trust_path)
    _check_schema(trust, "trust-policy.v1.json")
    names = ["assessment.v1.json", "authorization.v1.json", "execution.v1.json", "review.v1.json"]
    records = {name: _load_json(bundle / name) for name in names}
    for name, record in records.items():
        _check_schema(record, name)
    snapshot = _load_json(bundle / "workspace-snapshot.v1.json")
    _check_schema(snapshot, "workspace-snapshot.v1.json")
    snapshot_root = snapshot.pop("snapshot_root", None)
    if not snapshot_root or canonical_hash(snapshot, config) != snapshot_root:
        raise PrfDevError("snapshot commitment mismatch")
    authorization = records["authorization.v1.json"]
    execution = records["execution.v1.json"]
    review = records["review.v1.json"]
    policy_digest = canonical_hash(trust, config)
    if authorization.get("trust_policy_root") != policy_digest:
        raise PrfDevError("authorization is not bound to the independently supplied trust policy")
    if execution.get("authorization_root") != _record_digest(authorization, config):
        raise PrfDevError("execution is not bound to the approved authorization")
    if review.get("execution_root") != _record_digest(execution, config):
        raise PrfDevError("review is not bound to the exact execution record")
    if review.get("verdict") != "approved":
        raise PrfDevError("independent review did not approve this execution")
    if authorization.get("snapshot_root") != snapshot_root or execution.get("snapshot_root") != snapshot_root:
        raise PrfDevError("records do not bind to the same workspace snapshot")
    if authorization.get("repository_id") != execution.get("repository_id"):
        raise PrfDevError("authorization and execution repository identities differ")
    if authorization.get("target_revision") != execution.get("target_revision") or authorization.get("source_revision") != execution.get("source_revision"):
        raise PrfDevError("execution source or target revision differs from authorization")
    if review.get("snapshot_root") != snapshot_root:
        raise PrfDevError("review does not bind the exact workspace snapshot")
    if authorization.get("operations") != execution.get("operations"):
        raise PrfDevError("execution operations exceed or differ from authorization")
    evidence_records = []
    for item in execution.get("evidence", []):
        rel = item.get("path")
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise PrfDevError("evidence paths must be relative and remain inside the evidence bundle")
        artifact = bundle / rel
        resolved = artifact.resolve()
        if bundle not in resolved.parents or artifact.is_symlink() or not artifact.is_file():
            raise PrfDevError(f"evidence artifact missing or unsafe: {rel}")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if digest != item.get("sha256"):
            raise PrfDevError(f"evidence digest mismatch: {rel}")
        evidence_records.append({"path": rel, "sha256": digest})
    if canonical_hash(evidence_records, config) != execution.get("evidence_manifest_root"):
        raise PrfDevError("evidence manifest root mismatch")
    now = dt.datetime.now(dt.timezone.utc)
    used = set()
    scope = {"repository_id": authorization["repository_id"], "source_ref": authorization["source_ref"],
             "target_ref": authorization["target_ref"], "operations": authorization["operations"]}
    for name, role in [("authorization.v1.json", "repair-authorizer"),
                       ("execution.v1.json", "repair-executor"),
                       ("review.v1.json", "independent-reviewer")]:
        _check_signature(records[name], bundle, trust, config, role, used, now, scope)
    if authorization.get("authority_claim", False):
        principals = set()
        for sig in authorization.get("governance_signatures", []):
            principal = sig.get("principal")
            if principal in principals or principal in used:
                raise PrfDevError("governance approval identities must be distinct")
            principals.add(principal)
            _check_signature({"signature": sig}, bundle, trust, config, "governance-member", used, now, scope)
        threshold = trust.get("governance_threshold")
        if threshold != 3 or len(principals) < threshold:
            raise PrfDevError("authority-bearing acceptance requires the independently configured three-member threshold")
    if records["assessment.v1.json"].get("snapshot_root") != snapshot_root:
        raise PrfDevError("assessment does not refer to this workspace snapshot")
    claims = [
        {"claim": "snapshot-input-identity", "status": "verified", "required": True},
        {"claim": "authorization-signature-and-scope", "status": "verified", "required": True},
        {"claim": "execution-record-authentication", "status": "verified", "required": True},
        {"claim": "independent-review", "status": "verified", "required": True},
        {"claim": "evidence-digests", "status": "verified", "required": True},
        {"claim": "historical-source-completeness", "status": "unknown", "required": False},
        {"claim": "process-identity-beyond-signing-credential", "status": "unknown", "required": False},
    ]
    result = {"schema": "repair-verification.v1", "status": "verified-with-limitations", "artifact": str(bundle), "claims": claims,
              "trust_policy_root": policy_digest,
              "claim_boundary": "signatures authenticate credentials and scoped records, not process identity or historical truth"}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _emit_integration_report(value: dict, output: str | None = None, repo_path: str | None = None) -> int:
    serialized = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output:
        path = Path(output).expanduser().resolve()
        if repo_path and path.is_relative_to(Path(repo_path).expanduser().resolve()):
            raise PrfDevError("integration reports must be written outside the checked-out repository")
        try:
            path.write_text(serialized, encoding="utf-8")
        except OSError as exc:
            raise PrfDevError(f"cannot write integration report: {exc}") from exc
    print(serialized, end="")
    return 0 if value.get("status") in {"ready", "promoted", "verified"} else 2


def cmd_integration_preflight(args: argparse.Namespace) -> int:
    repo = JJRepository(args.repo)
    result = integration_preflight(repo, args.source_ref, args.target_ref, args.admission_record)
    return _emit_integration_report(result, args.output, args.repo)


def cmd_integration_promote(args: argparse.Namespace) -> int:
    try:
        report = json.loads(Path(args.preflight_report).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrfDevError("cannot read valid integration preflight report") from exc
    source_ref = report.get("source", {}).get("ref")
    target_ref = report.get("target", {}).get("ref")
    if not source_ref or not target_ref:
        raise PrfDevError("preflight report does not name source and target refs")
    result = integration_promote(JJRepository(args.repo), source_ref, target_ref, args.admission_record,
                                 args.preflight_report, args.preflight_sha256, args.record)
    return _emit_integration_report(result, args.output, args.repo)


def cmd_integration_verify(args: argparse.Namespace) -> int:
    result = integration_verify(JJRepository(args.repo), args.record)
    return _emit_integration_report(result, args.output, args.repo)


def cmd_integration_trigger(args: argparse.Namespace) -> int:
    result = integration_trigger(JJRepository(args.repo), args.source_ref, args.target_ref,
                                 args.review_decision, args.staging_receipt, args.issue,
                                 args.reviewer, args.records_dir)
    serialized = json.dumps(result, indent=2, sort_keys=True)
    print(serialized)
    return 0 if result.get("status") in {"promoted", "already-promoted"} else 2


def cmd_routine_submit(args: argparse.Namespace) -> int:
    _check_schema(_load_json(Path(args.trust_policy)), "routine-trust-policy.v1.json")
    value = routine_submission(JJRepository(args.repo), repository_id=args.repository_id, issue=args.issue,
        base_ref=args.base_ref, candidate_ref=args.candidate_ref, principal=args.signer,
        key=Path(args.key), evidence=Path(args.evidence), trust_path=Path(args.trust_policy), output=Path(args.output))
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def cmd_routine_review(args: argparse.Namespace) -> int:
    _check_routine_record(Path(args.submission), "submission")
    _check_schema(_load_json(Path(args.trust_policy)), "routine-trust-policy.v1.json")
    value = routine_review(Path(args.submission), verdict=args.verdict, principal=args.signer,
        key=Path(args.key), evidence=Path(args.evidence), trust_path=Path(args.trust_policy), output=Path(args.output))
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def cmd_routine_stage(args: argparse.Namespace) -> int:
    _check_routine_record(Path(args.submission), "submission")
    _check_routine_record(Path(args.review), "review")
    _check_schema(_load_json(Path(args.trust_policy)), "routine-trust-policy.v1.json")
    try:
        tests = json.loads(Path(args.tests).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrfDevError("--tests must be JSON array of passed qualification command strings") from exc
    if not isinstance(tests, list) or any(not isinstance(test, str) for test in tests):
        raise PrfDevError("--tests must be JSON array of passed qualification command strings")
    value = routine_staging(JJRepository(args.repo), Path(args.submission), Path(args.review),
        staging_base_ref=args.staging_base_ref, staged_ref=args.staged_ref, principal=args.signer,
        key=Path(args.key), tests=tests, trust_path=Path(args.trust_policy), output=Path(args.output))
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def cmd_routine_verify(args: argparse.Namespace) -> int:
    _check_routine_record(Path(args.submission), "submission")
    _check_routine_record(Path(args.review), "review")
    _check_routine_record(Path(args.staging_receipt), "staging")
    _check_schema(_load_json(Path(args.trust_policy)), "routine-trust-policy.v1.json")
    value = routine_verify(JJRepository(args.repo), Path(args.submission), Path(args.review), Path(args.staging_receipt),
        trust_path=Path(args.trust_policy), repository_id=args.repository_id, issue=args.issue,
        source_ref=args.candidate_ref, target_ref=args.staging_ref)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0 if value["status"] == "verified" else 1


def _check_routine_record(path: Path, kind: str) -> None:
    schema = _load_json(SCHEMA_DIR / "routine-records.v1.json")
    _validate_schema_value(_load_json(path), schema["$defs"][kind], f"routine {kind}", schema)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prf-dev", description="Reproducible, inspectable PRF workflows across repositories")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="check environment suitability without changing repositories")
    doctor.add_argument("--profile", choices=["core", "evm", "proving"], default="core")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)
    init = sub.add_parser("init", help="create private local configuration and XDG state/cache directories")
    init.add_argument("--workspace-root")
    init.set_defaults(func=cmd_init)
    snapshot = sub.add_parser("snapshot", help="record exact cross-repository inputs")
    snapshot.add_argument("--profile", choices=["core", "evm", "proving"], default="core")
    snapshot.add_argument("--strict", action="store_true", help="reject missing or dirty source checkouts")
    snapshot.add_argument("--output")
    snapshot.set_defaults(func=cmd_snapshot)
    reproduce = sub.add_parser("reproduce", help="re-run a pinned example in an isolated OCI container")
    reproduce.add_argument("case")
    reproduce.add_argument("--isolated", action="store_true", required=True)
    reproduce.set_defaults(func=cmd_reproduce)
    verify = sub.add_parser("verify", help="offline, non-executing evidence verification")
    verify.add_argument("artifact")
    verify.add_argument("--offline", action="store_true", required=True)
    verify.add_argument("--trust-policy")
    verify.set_defaults(func=cmd_verify)
    integration = sub.add_parser("integration", help="qualify and promote reviewed staging candidates into integration")
    integration_sub = integration.add_subparsers(dest="integration_command", required=True)
    preflight = integration_sub.add_parser("preflight", help="read-only staging-to-integration qualification")
    preflight.add_argument("--repo", required=True, help="PRF JJ workspace path")
    preflight.add_argument("--source-ref", default="agent-c-staging")
    preflight.add_argument("--target-ref", default="integration")
    preflight.add_argument("--admission-record", required=True, help="review/staging/validation evidence JSON")
    preflight.add_argument("--output", help="write the deterministic report outside the repository")
    preflight.set_defaults(func=cmd_integration_preflight)
    promote_cmd = integration_sub.add_parser("promote", help="fast-forward integration to a qualified staging candidate")
    promote_cmd.add_argument("--repo", required=True)
    promote_cmd.add_argument("--preflight-report", required=True)
    promote_cmd.add_argument("--preflight-sha256", required=True)
    promote_cmd.add_argument("--admission-record", required=True)
    promote_cmd.add_argument("--record", required=True, help="new durable promotion record path outside the repository")
    promote_cmd.add_argument("--output")
    promote_cmd.set_defaults(func=cmd_integration_promote)
    verify_cmd = integration_sub.add_parser("verify", help="verify a recorded integration promotion")
    verify_cmd.add_argument("--repo", required=True)
    verify_cmd.add_argument("--record", required=True)
    verify_cmd.add_argument("--output")
    verify_cmd.set_defaults(func=cmd_integration_verify)
    trigger_cmd = integration_sub.add_parser(
        "trigger",
        help="one-shot promotion; may move the integration bookmark when all checks pass",
    )
    trigger_cmd.add_argument(
        "--repo",
        required=True,
        help="JJ workspace containing the source and target bookmarks; this command can move the target",
    )
    trigger_cmd.add_argument("--source-ref", default="agent-c-staging")
    trigger_cmd.add_argument("--target-ref", default="integration")
    trigger_cmd.add_argument("--review-decision", required=True)
    trigger_cmd.add_argument("--staging-receipt", required=True)
    trigger_cmd.add_argument("--issue", required=True)
    trigger_cmd.add_argument("--reviewer", required=True)
    trigger_cmd.add_argument("--records-dir", required=True, help="auditable records directory outside the checkout")
    trigger_cmd.set_defaults(func=cmd_integration_trigger)
    routine = sub.add_parser("routine", help="signed routine submission, review, staging and verification")
    rs = routine.add_subparsers(dest="routine_command", required=True)
    submit = rs.add_parser("submit")
    for name in ("repo", "repository-id", "issue", "base-ref", "candidate-ref", "signer", "key", "evidence", "trust-policy", "output"):
        submit.add_argument("--" + name, required=True)
    submit.set_defaults(func=cmd_routine_submit)
    review_cmd = rs.add_parser("review")
    review_cmd.add_argument("--submission", required=True); review_cmd.add_argument("--verdict", choices=["approved", "rejected"], required=True)
    for name in ("signer", "key", "evidence", "trust-policy", "output"):
        review_cmd.add_argument("--" + name, required=True)
    review_cmd.set_defaults(func=cmd_routine_review)
    stage_cmd = rs.add_parser("stage")
    for name in ("repo", "submission", "review", "staging-base-ref", "staged-ref", "signer", "key", "tests", "trust-policy", "output"):
        stage_cmd.add_argument("--" + name, required=True)
    stage_cmd.set_defaults(func=cmd_routine_stage)
    verify_cmd = rs.add_parser("verify")
    for name in ("repo", "repository-id", "issue", "candidate-ref", "staging-ref", "submission", "review", "staging-receipt", "trust-policy"):
        verify_cmd.add_argument("--" + name, required=True)
    verify_cmd.set_defaults(func=cmd_routine_verify)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except (PrfDevError, IntegrationError, RoutineError) as exc:
        print(f"prf-dev: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
