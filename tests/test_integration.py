from __future__ import annotations

import fcntl
import json
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from prf_dev import integration


SOURCE = "a" * 40
TARGET = "b" * 40
TREE = "c" * 40
DIGEST = "d" * 64


class FakeRepo:
    def __init__(self, root: Path):
        self.root = root.resolve()
        (self.root / ".jj" / "repo").mkdir(parents=True)
        (self.root / ".jj" / "repo").mkdir(exist_ok=True)
        self.refs = {"agent-c-staging": SOURCE, "integration": TARGET}
        self.trees = {SOURCE: TREE, TARGET: "e" * 40}
        self.included = ["1" * 40, "2" * 40]
        self.reverse = []
        self.conflicts = set()
        self.operations = set()

    def resolve(self, ref):
        return self.refs[ref]

    def tree(self, commit):
        return self.trees[commit]

    def between(self, ancestor, descendant):
        if (ancestor, descendant) == (TARGET, SOURCE):
            return list(self.included)
        if (ancestor, descendant) == (SOURCE, TARGET):
            return list(self.reverse)
        return []

    def conflict(self, commit):
        return commit in self.conflicts

    def change(self, commit):
        return {"commit": commit, "description": "qualified change", "files": ["src/example"],
                "conflicted": self.conflict(commit)}

    def move_bookmark(self, name, commit):
        self.refs[name] = commit

    def operation_id(self):
        self.operations.add("f" * 40)
        return "f" * 40

    def op_exists(self, operation_id):
        return operation_id in self.operations


class IntegrationPromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = FakeRepo(self.root / "repo")
        self.admission = self.root / "admission.json"
        self.report = self.root / "preflight.json"
        self.record = self.root / "promotion.json"
        self.review_evidence = self.root / "review.json"
        self.staging_evidence = self.root / "staging.json"
        self.other_evidence = self.root / "qualification.json"
        for path, content in [(self.review_evidence, b"review"), (self.staging_evidence, b"stage"),
                              (self.other_evidence, b"tests")]:
            path.write_bytes(content)
        self.admission.write_text(json.dumps({
            "schema": "prf-dev.integration-admission.v1", "status": "admitted",
            "source_commit": SOURCE, "target_commit": TARGET,
            "reviewed_commits": self.repo.included,
            "review": {"status": "approved", "candidate_commit": SOURCE, "reviewer": "reviewer-1",
                       "ref": "review.json", "sha256": integration._sha(b"review")},
            "staging": {"status": "admitted", "source_commit": SOURCE, "target_commit": TARGET,
                        "ref": "staging.json", "sha256": integration._sha(b"stage")},
            "qualification": {"status": "passed", "commit": SOURCE, "tree": TREE,
                              "tests": [{"status": "passed", "tree": TREE, "command": "unit tests"}]},
            "issues": ["PRO-94"], "evidence": [{"ref": "qualification.json", "sha256": integration._sha(b"tests")}],
        }))

    def tearDown(self):
        self.temp.cleanup()

    def write_preflight(self):
        plan = integration.preflight(self.repo, "agent-c-staging", "integration", self.admission)
        self.report.write_text(json.dumps(plan, sort_keys=True))
        return plan

    def test_preflight_records_exact_fast_forward_and_tree(self):
        plan = self.write_preflight()
        self.assertEqual("ready", plan["status"])
        self.assertEqual(SOURCE, plan["source"]["commit"])
        self.assertEqual(TREE, plan["source"]["tree"])
        self.assertEqual(self.repo.included, [item["commit"] for item in plan["included_changes"]])
        self.assertEqual(plan["report_sha256"], integration._sha(integration._json_bytes({k:v for k,v in plan.items() if k != "report_sha256"})))

    def test_reverse_non_ancestor_blocks(self):
        self.repo.reverse = ["9" * 40]
        self.assertEqual("blocked", self.write_preflight()["status"])

    def test_conflicted_included_change_blocks(self):
        self.repo.conflicts.add(self.repo.included[0])
        plan = self.write_preflight()
        self.assertEqual("blocked", plan["status"])
        self.assertTrue(any("conflicts" in item for item in plan["blockers"]))

    def test_changed_evidence_digest_blocks(self):
        admission = json.loads(self.admission.read_text())
        admission["evidence"][0]["sha256"] = DIGEST
        self.admission.write_text(json.dumps(admission))
        self.assertTrue(any("digest mismatch" in item for item in self.write_preflight()["blockers"]))

    def test_missing_review_blocks(self):
        admission = json.loads(self.admission.read_text())
        admission["review"] = None
        self.admission.write_text(json.dumps(admission))
        self.assertTrue(any("review" in item for item in self.write_preflight()["blockers"]))

    def test_malformed_nested_evidence_blocks_without_crashing(self):
        admission = json.loads(self.admission.read_text())
        admission["review"] = ["not", "an", "object"]
        admission["staging"] = "malformed"
        admission["qualification"] = None
        self.admission.write_text(json.dumps(admission))
        plan = self.write_preflight()
        self.assertEqual("blocked", plan["status"])
        self.assertTrue(any("v1 contract" in item for item in plan["blockers"]))

    def test_failed_or_wrong_tree_validation_blocks(self):
        admission = json.loads(self.admission.read_text())
        admission["qualification"]["tests"][0]["status"] = "failed"
        self.admission.write_text(json.dumps(admission))
        self.assertTrue(any("validation" in item for item in self.write_preflight()["blockers"]))

    def test_eligibility_record_with_divergence_and_no_review_blocks(self):
        self.admission.write_text(json.dumps({"schema": "symphony-issue-eligibility.v1", "issue": "PRO-94",
            "last_check": {"status": "diverged", "reason": "workspace_base_diverged_from_staging"},
            "review": None, "staging": None}))
        plan = self.write_preflight()
        self.assertEqual("blocked", plan["status"])
        self.assertGreaterEqual(len(plan["blockers"]), 3)

    def test_stale_preflight_blocks_after_source_moves(self):
        plan = self.write_preflight()
        self.repo.refs["agent-c-staging"] = "7" * 40
        self.repo.trees["7" * 40] = "8" * 40
        with self.assertRaisesRegex(integration.IntegrationError, "changed since preflight"):
            integration.promote(self.repo, "agent-c-staging", "integration", self.admission,
                                self.report, plan["report_sha256"], self.record)
        self.assertEqual(TARGET, self.repo.refs["integration"])

    def test_lock_contention_blocks(self):
        plan = self.write_preflight()
        lock_path = integration._repo_lock(self.repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(integration.IntegrationError, "holds the repository lock"):
                integration.promote(self.repo, "agent-c-staging", "integration", self.admission,
                                    self.report, plan["report_sha256"], self.record)

    def test_successful_promotion_records_operation_and_verifies(self):
        plan = self.write_preflight()
        record = integration.promote(self.repo, "agent-c-staging", "integration", self.admission,
                                     self.report, plan["report_sha256"], self.record)
        self.assertEqual("promoted", record["status"])
        self.assertEqual(SOURCE, self.repo.refs["integration"])
        self.assertEqual("f" * 40, record["jj_operation_id"])
        self.assertEqual("verified", integration.verify(self.repo, self.record)["status"])

    def test_record_cannot_be_reused_or_overwritten(self):
        plan = self.write_preflight()
        self.record.write_text("existing")
        with self.assertRaisesRegex(integration.IntegrationError, "cannot create promotion record"):
            integration.promote(self.repo, "agent-c-staging", "integration", self.admission,
                                self.report, plan["report_sha256"], self.record)
        self.assertEqual(TARGET, self.repo.refs["integration"])

    def test_automatic_trigger_promotes_exact_reviewed_fast_forward_and_is_idempotent(self):
        reviewer = self.root / "review-decision.json"
        stage = self.root / "staging-receipt.json"
        log = self.root / "validation.log"
        log.write_text("unit tests passed\n")
        reviewer.write_text(json.dumps({
            "schema": "symphony-pilot-review-decision.v1", "issue": "PRO-94", "verdict": "approved",
            "candidate_sha": SOURCE, "base_sha": TARGET, "reviewed_tests": ["unit tests"],
            "finding": "Reviewed the exact immutable candidate and found no blocking defects.",
        }))
        stage.write_text(json.dumps({
            "schema": "symphony-staging-receipt.v1", "issue": "PRO-94", "new_staging_sha": SOURCE,
            "candidate_sha": SOURCE, "base_sha": TARGET, "staging_bookmark": "agent-c-staging",
            "old_staging_sha": TARGET, "validated_tree": TREE, "integration_mode": "fast-forward",
            "candidate_commits": [{"sha": commit} for commit in self.repo.included],
            "tests": ["unit tests"], "log": str(log),
        }))
        result = integration.trigger(self.repo, "agent-c-staging", "integration", reviewer, stage,
                                     "PRO-94", "reviewer-1", self.root / "records")
        self.assertEqual("promoted", result["status"])
        self.assertEqual(SOURCE, self.repo.refs["integration"])
        self.assertEqual("verified", result["verification"]["status"])
        repeated = integration.trigger(self.repo, "agent-c-staging", "integration", reviewer, stage,
                                       "PRO-94", "reviewer-1", self.root / "records")
        self.assertEqual("already-promoted", repeated["status"])

    def test_automatic_trigger_blocks_unreviewed_staging_commit(self):
        reviewer = self.root / "review-decision.json"
        stage = self.root / "staging-receipt.json"
        log = self.root / "validation.log"
        log.write_text("unit tests passed\n")
        reviewer.write_text(json.dumps({
            "schema": "symphony-pilot-review-decision.v1", "issue": "PRO-94", "verdict": "approved",
            "candidate_sha": SOURCE, "base_sha": TARGET, "reviewed_tests": ["unit tests"], "finding": "Reviewed candidate.",
        }))
        stage.write_text(json.dumps({
            "schema": "symphony-staging-receipt.v1", "issue": "PRO-94", "new_staging_sha": SOURCE,
            "candidate_sha": SOURCE, "base_sha": TARGET, "staging_bookmark": "agent-c-staging",
            "old_staging_sha": TARGET, "validated_tree": TREE, "integration_mode": "fast-forward",
            "candidate_commits": [{"sha": self.repo.included[0]}], "tests": ["unit tests"], "log": str(log),
        }))
        result = integration.trigger(self.repo, "agent-c-staging", "integration", reviewer, stage,
                                     "PRO-94", "reviewer-1", self.root / "records")
        self.assertEqual("blocked", result["status"])
        self.assertIn("exactly cover", result["blockers"][0])
        self.assertEqual(TARGET, self.repo.refs["integration"])


if __name__ == "__main__":
    unittest.main()
