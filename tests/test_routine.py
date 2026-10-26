import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from prf_dev.integration import JJRepository
from prf_dev import routine


def run(args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class RoutineAttestationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo_path = self.root / "repo"
        run(["jj", "git", "init", "--colocate", str(self.repo_path)])
        self.repo = JJRepository(self.repo_path)
        run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "new"])
        # Capture a stable base, then create one candidate change and an empty successor for staging.
        base = self.repo.resolve("@-")
        (self.repo_path / "change.txt").write_text("candidate\n")
        run(["jj", "-R", str(self.repo_path), "new"])
        self.candidate = self.repo.resolve("@-")
        run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "bookmark", "create", "test-base", "-r", base])
        run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "bookmark", "create", "candidate", "-r", self.candidate])
        run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "bookmark", "create", "staging", "-r", "@"])
        self.keys = {}
        principals = {}
        for name, role in [("coder", "routine-coder"), ("reviewer", "routine-reviewer"), ("stager", "routine-stager")]:
            private = self.root / f"{name}_key"
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(private)])
            pub = private.with_suffix(".pub").read_text().strip()
            self.keys[name] = private
            principals[name] = {"public_key": " ".join(pub.split()[:2]), "roles": [role],
                                "repositories": ["fixture"], "status": "active"}
        self.policy = self.root / "trust.json"
        self.policy.write_text(json.dumps({"schema": "prf-dev.routine-trust-policy.v1", "policy_id": "fixture-v1",
                                           "principals": principals}))
        self.evidence = self.root / "evidence.txt"
        self.evidence.write_text("reviewed fixture evidence\n")
        self.submission = self.root / "submission.json"
        self.review = self.root / "review.json"
        self.stage = self.root / "stage.json"
        routine.submission(self.repo, repository_id="fixture", issue="PRO-106", base_ref="test-base",
            candidate_ref="candidate", principal="coder", key=self.keys["coder"], evidence=self.evidence,
            trust_path=self.policy, output=self.submission)
        routine.review(self.submission, verdict="approved", principal="reviewer", key=self.keys["reviewer"],
            evidence=self.evidence, trust_path=self.policy, output=self.review)
        routine.staging(self.repo, self.submission, self.review, staging_base_ref="test-base", staged_ref="staging",
            principal="stager", key=self.keys["stager"], tests=["python fixture qualification"], trust_path=self.policy,
            output=self.stage)

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, *, issue="PRO-106", source="candidate", target="staging", trust=None):
        return routine.verify(self.repo, self.submission, self.review, self.stage,
            trust_path=trust or self.policy, repository_id="fixture", issue=issue,
            source_ref=source, target_ref=target)

    def test_signed_candidate_review_and_staging_verify_offline(self):
        result = self.verify()
        self.assertEqual("verified", result["status"])
        self.assertEqual(["coder", "reviewer", "stager"], [result[k] for k in ("coder", "reviewer", "stager")])
        command = [sys.executable, str(ROOT / "bin/prf-dev"), "routine", "verify", "--repo", str(self.repo_path),
            "--repository-id", "fixture", "--issue", "PRO-106", "--candidate-ref", "candidate",
            "--staging-ref", "staging", "--submission", str(self.submission), "--review", str(self.review),
            "--staging-receipt", str(self.stage), "--trust-policy", str(self.policy)]
        cli_result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(0, cli_result.returncode, cli_result.stderr)
        self.assertEqual("verified", json.loads(cli_result.stdout)["status"])

    def test_swapped_issue_and_wrong_key_policy_fail(self):
        with self.assertRaisesRegex(routine.RoutineError, "issue"):
            self.verify(issue="PRO-999")
        wrong = self.root / "wrong.json"
        value = json.loads(self.policy.read_text())
        value["principals"]["reviewer"]["public_key"] = value["principals"]["coder"]["public_key"]
        wrong.write_text(json.dumps(value))
        with self.assertRaisesRegex(routine.RoutineError, "Could not verify signature"):
            self.verify(trust=wrong)
        with self.assertRaisesRegex(routine.RoutineError, "repository"):
            routine.verify(self.repo, self.submission, self.review, self.stage, trust_path=self.policy,
                repository_id="other-repo", issue="PRO-106", source_ref="candidate", target_ref="staging")

    def test_candidate_mutation_invalidates_commit_binding(self):
        with self.assertRaisesRegex(routine.RoutineError, "candidate commit/tree"):
            self.verify(source="test-base")

    def test_modified_review_and_self_review_fail_closed(self):
        record = json.loads(self.review.read_text())
        record["verdict"] = "rejected"
        self.review.write_text(json.dumps(record))
        with self.assertRaisesRegex(routine.RoutineError, "payload digest"):
            self.verify()

    def test_self_review_and_substituted_staging_receipt_fail(self):
        policy = json.loads(self.policy.read_text())
        policy["principals"]["coder"]["roles"].append("routine-reviewer")
        self.policy.write_text(json.dumps(policy))
        self.submission.unlink(); self.submission.with_name(self.submission.name + ".sig").unlink()
        routine.submission(self.repo, repository_id="fixture", issue="PRO-106", base_ref="test-base",
            candidate_ref="candidate", principal="coder", key=self.keys["coder"], evidence=self.evidence,
            trust_path=self.policy, output=self.submission)
        self.review.unlink(); self.review.with_name(self.review.name + ".sig").unlink()
        routine.review(self.submission, verdict="approved", principal="coder", key=self.keys["coder"],
            evidence=self.evidence, trust_path=self.policy, output=self.review)
        with self.assertRaisesRegex(routine.RoutineError, "coder cannot review their own submission"):
            routine.staging(self.repo, self.submission, self.review, staging_base_ref="test-base", staged_ref="staging",
                principal="stager", key=self.keys["stager"], tests=["python fixture qualification"], trust_path=self.policy,
                output=self.stage)
        self.review.unlink(); self.review.with_name(self.review.name + ".sig").unlink()
        routine.review(self.submission, verdict="approved", principal="reviewer", key=self.keys["reviewer"],
            evidence=self.evidence, trust_path=self.policy, output=self.review)
        self.stage.unlink(); self.stage.with_name(self.stage.name + ".sig").unlink()
        routine.staging(self.repo, self.submission, self.review, staging_base_ref="test-base", staged_ref="staging",
            principal="stager", key=self.keys["stager"], tests=["python fixture qualification"], trust_path=self.policy,
            output=self.stage)
        receipt = json.loads(self.stage.read_text())
        receipt["staging_result_tree"] = "0" * 40
        self.stage.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(routine.RoutineError, "payload digest"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
