import json
from pathlib import Path
import tempfile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from prf_dev import cli

class PublicationAndSchemaTests(unittest.TestCase):
    def test_all_allowlisted_files_exist(self):
        entries = [x for x in (ROOT / "PUBLICATION.allowlist").read_text().splitlines() if x]
        self.assertTrue(entries)
        self.assertTrue(all((ROOT / item).is_file() and not (ROOT / item).is_symlink() for item in entries))

    def test_all_json_artifacts_parse(self):
        for path in [*ROOT.rglob("*.json")]:
            with self.subTest(path=path.relative_to(ROOT)):
                json.loads(path.read_text())

    def test_snapshot_schema_requires_immutable_repository_identity(self):
        schema = json.loads((ROOT / "schemas/workspace-snapshot.v1.json").read_text())
        record = {"schema": "workspace-snapshot.v1", "profile": {}, "repositories": [],
                  "toolchain_sources": {}, "toolchain_catalog_sha256":"0"*64, "policy_roots": {},
                  "trust_policy_root":None, "procedure": {}, "claim_boundary": "identity only", "snapshot_root": "sha256:" + "0"*64}
        cli._validate_schema_value(record, schema, "snapshot", schema)
        record["unexpected"] = True
        with self.assertRaises(cli.PrfDevError):
            cli._validate_schema_value(record, schema, "snapshot", schema)

    def test_signature_schema_requires_signing_time_and_role_record_binding(self):
        schema = json.loads((ROOT / "schemas/authorization.v1.json").read_text())
        signature = {"principal":"test", "namespace":"repair-authorizer",
                     "payload_digest":"sha256:"+"0"*64, "file":"a.sig"}
        with self.assertRaises(cli.PrfDevError):
            cli._validate_schema_value(signature, schema["$defs"]["signature"], "signature", schema)

    def test_integration_gate_exposes_audit_preflight_promote_and_verify(self):
        audit = cli.build_parser().parse_args(["integration", "audit-workspaces", "--repo", "/tmp/repo"])
        self.assertEqual("audit-workspaces", audit.integration_command)
        self.assertIs(audit.func, cli.cmd_integration_audit_workspaces)
        args = cli.build_parser().parse_args(["integration", "preflight", "--repo", "/tmp/repo",
                                              "--admission-record", "/tmp/admission.json"])
        self.assertEqual("integration", args.command)
        self.assertEqual("preflight", args.integration_command)
        self.assertIs(args.func, cli.cmd_integration_preflight)
        promote = cli.build_parser().parse_args(["integration", "promote", "--repo", "/tmp/repo",
            "--preflight-report", "/tmp/report.json", "--preflight-sha256", "0"*64,
            "--admission-record", "/tmp/admission.json", "--record", "/tmp/promotion.json"])
        self.assertIs(promote.func, cli.cmd_integration_promote)
        verify = cli.build_parser().parse_args(["integration", "verify", "--repo", "/tmp/repo",
            "--record", "/tmp/promotion.json"])
        self.assertIs(verify.func, cli.cmd_integration_verify)
        trigger = cli.build_parser().parse_args(["integration", "trigger", "--repo", "/tmp/repo",
            "--review-decision", "/tmp/review.json", "--staging-receipt", "/tmp/stage.json",
            "--issue", "PRO-94", "--reviewer", "reviewer-1", "--records-dir", "/tmp/records"])
        self.assertIs(trigger.func, cli.cmd_integration_trigger)

    def test_routine_attestation_exposes_submit_review_stage_and_verify(self):
        submit = cli.build_parser().parse_args(["routine", "submit", "--repo", "/tmp/repo",
            "--repository-id", "prf", "--issue", "PRO-123", "--base-ref", "base",
            "--candidate-ref", "candidate", "--signer", "coder", "--key", "/tmp/coder",
            "--evidence", "/tmp/evidence", "--trust-policy", "/tmp/trust.json", "--output", "/tmp/submission.json"])
        self.assertIs(submit.func, cli.cmd_routine_submit)
        verify = cli.build_parser().parse_args(["routine", "verify", "--repo", "/tmp/repo",
            "--repository-id", "prf", "--issue", "PRO-123", "--candidate-ref", "candidate",
            "--staging-ref", "staging", "--submission", "/tmp/submission.json",
            "--review", "/tmp/review.json", "--staging-receipt", "/tmp/stage.json",
            "--trust-policy", "/tmp/trust.json"])
        self.assertIs(verify.func, cli.cmd_routine_verify)

    def test_symlinked_artifact_is_not_accepted_as_regular_input(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); regular=root/"record.json"; regular.write_text("{}")
            alias=root/"alias.json"; alias.symlink_to(regular)
            with self.assertRaises(cli.PrfDevError): cli._load_json(alias)

if __name__ == "__main__": unittest.main()
