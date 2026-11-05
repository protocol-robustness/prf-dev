from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys
import shutil
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from prf_dev import integration


SOURCE = "a" * 40
TARGET = "b" * 40
TREE = "c" * 40
DIGEST = "d" * 64


def run(args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class FakeRepo:
    def __init__(self, root: Path):
        self.root = root.resolve()
        (self.root / ".jj" / "repo").mkdir(parents=True)
        (self.root / ".jj" / "repo").mkdir(exist_ok=True)
        self.refs = {"agent-c-staging": SOURCE, "integration": TARGET}
        self.trees = {SOURCE: TREE, TARGET: "e" * 40}
        self.included = ["1" * 40, "2" * 40]
        self.reverse = []
        self.ancestor = True
        self.conflicts = set()
        self.operations = set()

    def resolve(self, ref):
        return self.refs.get(ref, ref)

    def tree(self, commit):
        return self.trees[commit]

    def between(self, ancestor, descendant):
        if (ancestor, descendant) == (TARGET, SOURCE):
            return list(self.included)
        if (ancestor, descendant) == (SOURCE, TARGET):
            return list(self.reverse)
        return []

    def is_ancestor(self, ancestor, descendant):
        return self.ancestor

    def conflict(self, commit):
        return commit in self.conflicts

    def workspace_heads(self):
        return []

    def change(self, commit):
        return {"commit": commit, "description": "qualified change", "files": ["src/example"],
                "conflicted": self.conflict(commit)}

    def move_bookmark(self, name, expected_old, commit):
        if self.refs[name] != expected_old:
            raise integration.IntegrationError("conditional move failed")
        self.refs[name] = commit

    def operation_id(self):
        self.operations.add("f" * 128)
        return "f" * 128

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
        self.review_doc = {"schema": "prf-dev.integration-review.v1", "status": "approved",
                           "source_commit": SOURCE, "target_commit": TARGET, "source_tree": TREE,
                           "target_tree": "e" * 40, "reviewed_commits": self.repo.included,
                           "reviewer": "reviewer-1", "issues": ["PRO-94"]}
        self.stage_doc = {"schema": "prf-dev.staging-admission.v1", "status": "admitted",
                          "source_commit": SOURCE, "target_commit": TARGET, "source_tree": TREE,
                          "target_tree": "e" * 40, "included_commits": self.repo.included,
                          "issues": ["PRO-94"], "validation_evidence": [{
                              "command": "unit tests", "status": "passed", "tree": TREE,
                              "ref": "qualification.json", "sha256": integration._sha(b"tests"),
                          }]}
        review_bytes, stage_bytes = json.dumps(self.review_doc).encode(), json.dumps(self.stage_doc).encode()
        for path, content in [(self.review_evidence, review_bytes), (self.staging_evidence, stage_bytes),
                              (self.other_evidence, b"tests")]:
            path.write_bytes(content)
        self.admission.write_text(json.dumps({
            "schema": "prf-dev.integration-admission.v2", "status": "admitted",
            "source_commit": SOURCE, "source_tree": TREE, "target_commit": TARGET, "target_tree": "e" * 40,
            "reviewed_commits": self.repo.included,
            "review": {"status": "approved", "candidate_commit": SOURCE, "reviewer": "reviewer-1",
                       "reviewed_commits": self.repo.included,
                       "ref": "review.json", "sha256": integration._sha(review_bytes)},
            "staging": {"status": "admitted", "source_commit": SOURCE, "target_commit": TARGET,
                        "source_tree": TREE, "target_tree": "e" * 40, "included_commits": self.repo.included,
                        "ref": "staging.json", "sha256": integration._sha(stage_bytes)},
            "qualification": {"status": "passed", "commit": SOURCE, "tree": TREE,
                              "tests": [{"status": "passed", "tree": TREE, "command": "unit tests",
                                         "ref": "qualification.json", "sha256": integration._sha(b"tests")}]},
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
        self.assertEqual("prf-dev.integration-preflight.v2", plan["schema"])
        self.assertEqual("ready", plan["status"])
        self.assertEqual(SOURCE, plan["source"]["commit"])
        self.assertEqual(TREE, plan["source"]["tree"])
        self.assertEqual("clear", plan["workspace_audit"]["status"])
        self.assertEqual(self.repo.included, [item["commit"] for item in plan["included_changes"]])
        self.assertEqual(plan["report_sha256"], integration._sha(integration._json_bytes({k:v for k,v in plan.items() if k != "report_sha256"})))

    def test_preflight_lineage_audit_uses_protected_staging_when_source_is_candidate_id(self):
        protected_staging = "f" * 40
        self.repo.refs["agent-c-staging"] = protected_staging
        plan = integration.preflight(self.repo, SOURCE, "integration", self.admission)
        self.assertEqual(SOURCE, plan["source"]["commit"])
        self.assertEqual("agent-c-staging", plan["workspace_audit"]["staging"]["ref"])
        self.assertEqual(protected_staging, plan["workspace_audit"]["staging"]["commit"])

    def test_reverse_non_ancestor_blocks(self):
        self.repo.reverse = ["9" * 40]
        self.repo.ancestor = False
        self.assertEqual("blocked", self.write_preflight()["status"])

    def test_empty_reverse_range_does_not_prove_ancestry(self):
        self.repo.ancestor = False
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

    def test_duplicate_issue_ids_block(self):
        admission = json.loads(self.admission.read_text())
        admission["issues"] = ["PRO-94", "PRO-94"]
        self.admission.write_text(json.dumps(admission))
        self.assertTrue(any("must be unique" in item for item in self.write_preflight()["blockers"]))

    def test_malformed_nested_evidence_blocks_without_crashing(self):
        admission = json.loads(self.admission.read_text())
        admission["review"] = ["not", "an", "object"]
        admission["staging"] = "malformed"
        admission["qualification"] = None
        self.admission.write_text(json.dumps(admission))
        plan = self.write_preflight()
        self.assertEqual("blocked", plan["status"])
        self.assertTrue(any("v2 contract" in item for item in plan["blockers"]))

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
        self.assertEqual("f" * 128, record["jj_operation_id"])
        self.assertEqual("verified", integration.verify(self.repo, self.record)["status"])

    def test_record_cannot_be_reused_or_overwritten(self):
        plan = self.write_preflight()
        self.record.write_text("existing")
        with self.assertRaisesRegex(integration.IntegrationError, "cannot create promotion record"):
            integration.promote(self.repo, "agent-c-staging", "integration", self.admission,
                                self.report, plan["report_sha256"], self.record)
        self.assertEqual(TARGET, self.repo.refs["integration"])

    def test_automatic_trigger_blocks_without_execution_receipts(self):
        reviewer = self.root / "review-decision.json"
        stage = self.root / "staging-receipt.json"
        log = self.root / "validation.log"
        log.write_text("unit tests passed\n")
        reviewer.write_text(json.dumps(self.review_doc))
        stage.write_text(json.dumps({
            **self.stage_doc, "validation_evidence": [],
        }))
        result = integration.trigger(self.repo, "agent-c-staging", "integration", reviewer, stage,
                                     "PRO-94", "reviewer-1", self.root / "records")
        self.assertEqual("blocked", result["status"])
        self.assertEqual(TARGET, self.repo.refs["integration"])

    def test_automatic_trigger_promotes_single_exact_candidate_with_execution_receipts(self):
        reviewer = self.root / "review-decision.json"
        stage = self.root / "staging-receipt.json"
        log = self.root / "validation.log"
        log.write_text("exact candidate validation completed\n")
        reviewer.write_text(json.dumps(self.review_doc))
        stage.write_text(json.dumps({
            **self.stage_doc, "validation_evidence": [{
                "command": "unit suite", "status": "passed", "tree": TREE,
                "ref": str(self.other_evidence), "sha256": integration._sha(b"tests"),
            }],
        }))
        result = integration.trigger(self.repo, "agent-c-staging", "integration", reviewer, stage,
                                     "PRO-94", "reviewer-1", self.root / "records")
        self.assertEqual("promoted", result["status"], result)
        self.assertEqual(SOURCE, self.repo.refs["integration"])
        admission = json.loads(Path(result["admission_record"]).read_text())
        self.assertEqual("prf-dev.integration-admission.v2", admission["schema"])
        self.assertEqual(self.repo.included, admission["reviewed_commits"])
        self.assertEqual(self.repo.included, admission["review"]["reviewed_commits"])
        self.assertEqual(TREE, admission["qualification"]["tests"][0]["tree"])
        self.assertEqual("verified", result["verification"]["status"])

    def test_automatic_trigger_rejects_legacy_issue_records_without_synthesizing_admission(self):
        reviewer = self.root / "legacy-review.json"
        stage = self.root / "legacy-stage.json"
        reviewer.write_text(json.dumps({
            "schema": "symphony-pilot-review-decision.v1", "issue": "PRO-94", "verdict": "approved",
            "candidate_sha": SOURCE, "base_sha": TARGET, "reviewed_tests": ["unit tests"],
            "finding": "Reviewed the complete candidate.",
        }))
        stage.write_text(json.dumps({
            "schema": "symphony-staging-receipt.v1", "issue": "PRO-94", "new_staging_sha": SOURCE,
            "candidate_sha": SOURCE, "base_sha": TARGET,
        }))
        records = self.root / "legacy-records"
        result = integration.trigger(self.repo, "agent-c-staging", "integration", reviewer, stage,
                                     "PRO-94", "reviewer-1", records)
        self.assertEqual("blocked", result["status"])
        self.assertEqual(TARGET, self.repo.refs["integration"])
        self.assertFalse(list(records.glob("admission-*.json")))

    def test_single_issue_trigger_rejects_aggregate_that_names_multiple_issues(self):
        reviewer = self.root / "review-decision.json"
        stage = self.root / "staging-receipt.json"
        review_doc = {**self.review_doc, "issues": ["PRO-94", "PRO-95"]}
        stage_doc = {**self.stage_doc, "issues": ["PRO-94", "PRO-95"], "validation_evidence": [{
            "command": "unit suite", "status": "passed", "tree": TREE,
            "ref": str(self.other_evidence), "sha256": integration._sha(b"tests"),
        }]}
        reviewer.write_text(json.dumps(review_doc))
        stage.write_text(json.dumps(stage_doc))
        records = self.root / "multi-issue-records"
        result = integration.trigger(self.repo, "agent-c-staging", "integration", reviewer, stage,
                                     "PRO-94", "reviewer-1", records)
        self.assertEqual("blocked", result["status"])
        self.assertEqual(TARGET, self.repo.refs["integration"])
        self.assertFalse(list(records.glob("admission-*.json")))

    def test_automatic_trigger_blocks_unreviewed_staging_commit(self):
        reviewer = self.root / "review-decision.json"
        stage = self.root / "staging-receipt.json"
        log = self.root / "validation.log"
        log.write_text("unit tests passed\n")
        reviewer.write_text(json.dumps(self.review_doc))
        stage.write_text(json.dumps({
            **self.stage_doc, "included_commits": [self.repo.included[0]], "validation_evidence": [],
        }))
        result = integration.trigger(self.repo, "agent-c-staging", "integration", reviewer, stage,
                                     "PRO-94", "reviewer-1", self.root / "records")
        self.assertEqual("blocked", result["status"])
        self.assertIn("exact ordered", result["blockers"][0])
        self.assertEqual(TARGET, self.repo.refs["integration"])


class WorkspaceLineageAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        old_xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        isolated_config_home = self.root / "config"
        isolated_config_home.mkdir()
        os.environ["XDG_CONFIG_HOME"] = str(isolated_config_home)
        self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME", None)
                        if old_xdg_config_home is None
                        else os.environ.__setitem__("XDG_CONFIG_HOME", old_xdg_config_home))
        self.repo_path = self.root / "repo"
        run(["jj", "git", "init", "--colocate", str(self.repo_path)])
        run(["jj", "-R", str(self.repo_path), "config", "set", "--repo", "user.name", "Fixture Agent"])
        run(["jj", "-R", str(self.repo_path), "config", "set", "--repo", "user.email", "agent@example.invalid"])
        self.repo = integration.JJRepository(self.repo_path)
        run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "new"])
        base = self.repo.resolve("@-")
        run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "bookmark", "create", "integration", "-r", base])
        run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "bookmark", "create", "agent-c-staging", "-r", base])
        self.agent_path = self.root / "agent-a"
        run(["jj", "-R", str(self.repo_path), "workspace", "add", "--name", "agent-a",
             "--revision", base, str(self.agent_path)])

    def tearDown(self):
        self.temp.cleanup()

    def commit_agent_change(self, file_name: str, content: str) -> str:
        (self.agent_path / file_name).write_text(content, encoding="utf-8")
        run(["jj", "-R", str(self.agent_path), "new"])
        return integration.JJRepository(self.agent_path).resolve("@-")

    def test_detects_agent_commit_missing_from_staging_and_integration(self):
        commit = self.commit_agent_change("feature.txt", "agent feature\n")
        report = integration.audit_workspaces(self.repo)
        self.assertEqual("attention", report["status"])
        entry = report["workspaces"][0]["unlanded_commits"][0]
        self.assertEqual(commit, entry["commit"])
        self.assertFalse(entry["in_staging"])
        self.assertFalse(entry["in_integration"])

    def test_reports_staged_but_not_yet_integrated_agent_commit(self):
        commit = self.commit_agent_change("feature.txt", "agent feature\n")
        run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "bookmark", "set",
             "agent-c-staging", "-r", commit])
        report = integration.audit_workspaces(self.repo)
        entry = report["workspaces"][0]["unlanded_commits"][0]
        self.assertTrue(entry["in_staging"])
        self.assertFalse(entry["in_integration"])

    def test_clears_after_agent_commit_reaches_both_protected_refs(self):
        commit = self.commit_agent_change("feature.txt", "agent feature\n")
        for ref in ("agent-c-staging", "integration"):
            run(["jj", "--ignore-working-copy", "-R", str(self.repo_path), "bookmark", "set", ref, "-r", commit])
        report = integration.audit_workspaces(self.repo)
        self.assertEqual("clear", report["status"])
        self.assertEqual([], report["workspaces"])

    def test_ignores_non_agent_workspaces(self):
        other_path = self.root / "review-workspace"
        base = self.repo.resolve("integration")
        run(["jj", "-R", str(self.repo_path), "workspace", "add", "--name", "review-workspace",
             "--revision", base, str(other_path)])
        (other_path / "review-only.txt").write_text("review\n", encoding="utf-8")
        run(["jj", "-R", str(other_path), "new"])
        report = integration.audit_workspaces(self.repo)
        self.assertEqual("clear", report["status"])
        self.assertEqual(1, report["workspaces_scanned"])
        self.assertEqual([], report["workspaces"])

    def test_includes_symphony_coding_workspaces(self):
        symphony_path = self.root / "symphony-coding"
        base = self.repo.resolve("integration")
        run(["jj", "-R", str(self.repo_path), "workspace", "add", "--name", "symphony-prf-coding-PRO-1",
             "--revision", base, str(symphony_path)])
        (symphony_path / "feature.txt").write_text("symphony agent feature\n", encoding="utf-8")
        run(["jj", "-R", str(symphony_path), "new"])
        report = integration.audit_workspaces(self.repo)
        names = {item["name"] for item in report["workspaces"]}
        self.assertIn("symphony-prf-coding-PRO-1", names)
class RealJJInspectionTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("jj") or not shutil.which("git"):
            self.skipTest("real JJ and Git are required")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                        XDG_CONFIG_HOME=str(Path(self.temp.name) / "config"))
        Path(self.env["XDG_CONFIG_HOME"]).mkdir()
        self.old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.env["XDG_CONFIG_HOME"]
        subprocess.run(["jj", "git", "init", "--colocate"], cwd=self.root, check=True,
                       text=True, capture_output=True, env=self.env)
        subprocess.run(["jj", "-R", str(self.root), "config", "set", "--repo", "user.name", "JJ Test"], check=True, env=self.env)
        subprocess.run(["jj", "-R", str(self.root), "config", "set", "--repo", "user.email", "jj-test@example.test"], check=True, env=self.env)
        self.git("config", "user.name", "JJ Test")
        self.git("config", "user.email", "jj-test@example.test")

    def tearDown(self):
        if self.old_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old_xdg
        self.temp.cleanup()

    def jj(self, *args):
        return subprocess.run(["jj", "--ignore-working-copy", "-R", str(self.root), *args], check=True, text=True, capture_output=True, env=self.env).stdout.strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, text=True, capture_output=True, env=self.env).stdout.strip()

    def write_commit(self, files, message):
        for name, value in files.items():
            (self.root / name).write_text(value)
        subprocess.run(["jj", "-R", str(self.root), "status"], check=True,
                       text=True, capture_output=True, env=self.env)
        self.jj("describe", "-m", message)
        commit = self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        self.jj("new")
        return commit

    def test_single_parent_and_two_parent_merge_union_and_resolution(self):
        base = self.write_commit({"base": "base\n", "common": "base\n"}, "base")
        one = self.write_commit({"single": "one\n"}, "single")
        repo = integration.JJRepository(self.root)
        single_commit = one
        single = repo.change(one)
        self.assertEqual(["A single"], single["files"])
        left = self.write_commit({"left": "left\n", "common": "left\n"}, "left")
        # Create right as a sibling of left, with its own changes to common.
        right_workspace = self.root.parent / "right-workspace"
        subprocess.run(["jj", "-R", str(self.root), "workspace", "add", "--name", "right",
                        "--revision", base, str(right_workspace)], check=True, capture_output=True, env=self.env)
        (right_workspace / "right").write_text("right\n")
        (right_workspace / "common").write_text("right\n")
        subprocess.run(["jj", "-R", str(right_workspace), "status"], check=True,
                       capture_output=True, env=self.env)
        subprocess.run(["jj", "--ignore-working-copy", "-R", str(right_workspace), "new", "-m", "right"],
                       check=True, capture_output=True, env=self.env)
        right = subprocess.run(["jj", "--ignore-working-copy", "-R", str(right_workspace), "log", "--no-graph",
                                "-r", "@-", "-T", "commit_id"], check=True, text=True,
                               capture_output=True, env=self.env).stdout.strip()
        subprocess.run(["jj", "-R", str(self.root), "new", left, right, "-m", "merge"],
                       check=True, capture_output=True, env=self.env)
        subprocess.run(["jj", "-R", str(self.root), "resolve", "--tool", ":ours", "common"],
                       check=False, capture_output=True, env=self.env)
        (self.root / "common").write_text("resolved\n")
        subprocess.run(["jj", "-R", str(self.root), "status"], check=False, capture_output=True, env=self.env)
        self.jj("describe", "-m", "resolution")
        merge = self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        change = repo.change(merge)
        self.assertEqual(["A left", "A right", "A single", "M common"], change["files"])
        self.assertFalse(change["conflicted"])
        self.assertEqual([single_commit, left, right, merge], repo.between(base, merge))
        self.assertTrue(repo.is_ancestor(base, merge))

    def test_commit_is_ancestor_of_its_child(self):
        first = self.write_commit({"one": "1"}, "first")
        repo = integration.JJRepository(self.root)
        self.assertTrue(repo.is_ancestor(first, repo.resolve("@")))

    def commit_files(self, files, message):
        for name, value in files.items():
            (self.root / name).write_text(value)
        subprocess.run(["jj", "-R", str(self.root), "status"], check=True, capture_output=True, env=self.env)
        subprocess.run(["jj", "-R", str(self.root), "describe", "-m", message], check=True, capture_output=True, env=self.env)
        commit = self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        subprocess.run(["jj", "-R", str(self.root), "new"], check=True, capture_output=True, env=self.env)
        return commit

    def bookmark(self, name, commit):
        subprocess.run(["jj", "--ignore-working-copy", "-R", str(self.root),
                        "bookmark", "set", name, "--revision", commit, "--allow-backwards"],
                       check=True, capture_output=True, env=self.env)

    def promotion_fixture(self):
        base = self.commit_files({"base.txt": "base\n"}, "qualified base")
        source = self.commit_files({"candidate.txt": "candidate\n"}, "staged candidate")
        self.bookmark("integration", base)
        self.bookmark("agent-c-staging", source)
        repo = integration.JJRepository(self.root)
        tree = repo.tree(source)
        target_tree = repo.tree(base)
        commits = repo.between(base, source)
        evidence_dir = self.root.parent / "evidence"
        evidence_dir.mkdir(exist_ok=True)
        review_body = json.dumps({"schema": "prf-dev.integration-review.v1", "status": "approved",
                                  "source_commit": source, "target_commit": base, "source_tree": tree,
                                  "target_tree": target_tree, "reviewed_commits": commits,
                                  "reviewer": "reviewer-1", "issues": ["PRO-94"]}).encode()
        validation_body = b"validation completed against exact candidate tree"
        validation_path = evidence_dir / "validation.json"
        staging_body = json.dumps({"schema": "prf-dev.staging-admission.v1", "status": "admitted",
                                   "source_commit": source, "target_commit": base, "source_tree": tree,
                                   "target_tree": target_tree, "included_commits": commits,
                                   "issues": ["PRO-94"], "validation_evidence": [{
                                       "command": "unit suite", "status": "passed", "tree": tree,
                                       "ref": str(validation_path), "sha256": integration._sha(validation_body),
                                   }]}).encode()
        evidence = {}
        for name, body in (("review", review_body), ("staging", staging_body),
                           ("validation", validation_body)):
            path = evidence_dir / f"{name}.json"
            path.write_bytes(body)
            evidence[name] = {"ref": str(path), "sha256": integration._sha(body)}
        admission = evidence_dir / "admission.json"
        admission.write_text(json.dumps({
            "schema": "prf-dev.integration-admission.v2", "status": "admitted",
            "source_commit": source, "source_tree": tree, "target_commit": base, "target_tree": target_tree,
            "reviewed_commits": commits,
            "review": {"status": "approved", "candidate_commit": source, "reviewer": "reviewer-1",
                       "reviewed_commits": commits, **evidence["review"]},
            "staging": {"status": "admitted", "source_commit": source, "target_commit": base,
                        "source_tree": tree, "target_tree": target_tree,
                        "included_commits": commits, **evidence["staging"]},
            "qualification": {"status": "passed", "commit": source, "tree": tree,
                              "tests": [{"command": "unit suite", "status": "passed", "tree": tree,
                                         **evidence["validation"]}]},
            "issues": ["PRO-94"], "evidence": [evidence["validation"]],
        }))
        report_path = evidence_dir / "preflight.json"
        record_path = evidence_dir / "promotion.json"
        report = integration.preflight(repo, "agent-c-staging", "integration", admission)
        report_path.write_text(json.dumps(report, sort_keys=True))
        return repo, base, source, admission, report_path, report["report_sha256"], record_path, evidence

    def test_real_jj_root_and_empty_changes(self):
        repo = integration.JJRepository(self.root)
        root_change = repo.change(repo.resolve("root()"))
        self.assertEqual([], root_change["files"])
        empty = self.jj("log", "--no-graph", "-r", "@", "-T", "commit_id")
        self.assertEqual([], repo.change(empty)["files"])

    def test_real_jj_divergent_and_unrelated_target_blocks(self):
        repo, base, source, admission, report_path, digest, record, _ = self.promotion_fixture()
        unrelated = self.commit_files({"unrelated.txt": "elsewhere\n"}, "unrelated")
        self.bookmark("integration", unrelated)
        report = integration.preflight(repo, "agent-c-staging", "integration", admission)
        self.assertEqual("blocked", report["status"])
        self.assertFalse(repo.is_ancestor(unrelated, source))
        self.assertEqual(unrelated, repo.resolve("integration"))
        self.assertEqual(source, repo.resolve("agent-c-staging"))

    def test_real_jj_stale_source_bookmark_blocks_without_target_move(self):
        repo, base, source, admission, report_path, digest, record, _ = self.promotion_fixture()
        moved = self.commit_files({"later.txt": "later\n"}, "staging moved")
        self.bookmark("agent-c-staging", moved)
        with self.assertRaisesRegex(integration.IntegrationError, "changed since preflight"):
            integration.promote(repo, "agent-c-staging", "integration", admission, report_path, digest, record)
        self.assertEqual(base, repo.resolve("integration"))
        self.assertEqual(moved, repo.resolve("agent-c-staging"))

    def test_real_jj_stale_target_bookmark_blocks_without_overwriting_it(self):
        repo, base, source, admission, report_path, digest, record, _ = self.promotion_fixture()
        moved = self.commit_files({"target-later.txt": "later\n"}, "integration moved")
        self.bookmark("integration", moved)
        with self.assertRaisesRegex(integration.IntegrationError, "changed since preflight"):
            integration.promote(repo, "agent-c-staging", "integration", admission, report_path, digest, record)
        self.assertEqual(moved, repo.resolve("integration"))
        self.assertEqual(source, repo.resolve("agent-c-staging"))

    def test_real_jj_stale_aggregate_validation_evidence_blocks(self):
        repo, base, source, admission, report_path, digest, record, evidence = self.promotion_fixture()
        Path(evidence["validation"]["ref"]).write_text("changed after qualification")
        with self.assertRaisesRegex(integration.IntegrationError, "changed since preflight"):
            integration.promote(repo, "agent-c-staging", "integration", admission, report_path, digest, record)
        self.assertEqual(base, repo.resolve("integration"))
        self.assertEqual(source, repo.resolve("agent-c-staging"))

    def test_real_jj_lock_contention_and_failed_conditional_move_do_not_promote(self):
        repo, base, source, admission, report_path, digest, record, _ = self.promotion_fixture()
        lock_path = integration._repo_lock(repo)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(integration.IntegrationError, "holds the repository lock"):
                integration.promote(repo, "agent-c-staging", "integration", admission, report_path, digest,
                                    record.with_name("lock-promotion.json"))
        self.assertEqual(base, repo.resolve("integration"))
        self.assertEqual(source, repo.resolve("agent-c-staging"))
        race_target = self.commit_files({"racer.txt": "racer\n"}, "external target move")
        original_move = repo.move_bookmark

        def race_then_cas(name, expected_old, commit):
            original_move(name, expected_old, race_target)
            original_move(name, expected_old, commit)

        repo.move_bookmark = race_then_cas
        with self.assertRaises(integration.IntegrationError):
            integration.promote(repo, "agent-c-staging", "integration", admission, report_path, digest,
                                record.with_name("cas-promotion.json"))
        failed = json.loads(record.with_name("cas-promotion.json").read_text())
        self.assertEqual("promotion-failed", failed["status"])
        self.assertEqual(race_target, repo.resolve("integration"))
        self.assertEqual(source, repo.resolve("agent-c-staging"))

    def test_real_jj_successful_promotion_has_exact_tree_and_operation_receipt(self):
        repo, base, source, admission, report_path, digest, record, _ = self.promotion_fixture()
        expected_tree = repo.tree(source)
        result = integration.promote(repo, "agent-c-staging", "integration", admission, report_path, digest, record)
        self.assertEqual("promoted", result["status"])
        self.assertEqual(source, repo.resolve("integration"))
        self.assertEqual({"ref": "integration", "commit": source, "tree": expected_tree}, result["result"])
        self.assertTrue(repo.op_exists(result["jj_operation_id"]))
        self.assertEqual("verified", integration.verify(repo, record)["status"])

    def test_real_jj_legacy_v1_admission_is_rejected(self):
        repo, base, source, admission, report_path, digest, record, _ = self.promotion_fixture()
        document = json.loads(admission.read_text())
        document["schema"] = "prf-dev.integration-admission.v1"
        admission.write_text(json.dumps(document))
        report = integration.preflight(repo, "agent-c-staging", "integration", admission)
        self.assertEqual("blocked", report["status"])
        self.assertIn("unsupported admission record schema", report["blockers"])
        self.assertEqual(base, repo.resolve("integration"))
        self.assertEqual(source, repo.resolve("agent-c-staging"))

    def test_real_jj_missing_or_stale_aggregate_review_and_stage_records_block(self):
        repo, base, source, admission, report_path, digest, record, evidence = self.promotion_fixture()
        original = json.loads(admission.read_text())
        cases = []
        missing_review = json.loads(json.dumps(original))
        missing_review["review"]["reviewed_commits"] = []
        cases.append(missing_review)
        stale_stage = json.loads(json.dumps(original))
        stale_stage["staging"]["included_commits"] = []
        cases.append(stale_stage)
        stale_target_tree = json.loads(json.dumps(original))
        stale_target_tree["target_tree"] = "0" * 40
        cases.append(stale_target_tree)
        for invalid in cases:
            admission.write_text(json.dumps(invalid))
            report = integration.preflight(repo, "agent-c-staging", "integration", admission)
            self.assertEqual("blocked", report["status"])
            self.assertEqual(base, repo.resolve("integration"))
            self.assertEqual(source, repo.resolve("agent-c-staging"))

    def test_real_jj_malformed_parent_and_diff_output_fail_closed(self):
        repo, base, source, admission, report_path, digest, record, _ = self.promotion_fixture()
        real_jj = repo.jj

        def malformed_parent(*args):
            if args and args[-1].startswith("parents.map"):
                return "not-a-commit-id"
            return real_jj(*args)

        with patch.object(repo, "jj", side_effect=malformed_parent):
            with self.assertRaisesRegex(integration.IntegrationError, "malformed parent identities"):
                repo.change(source)

        def malformed_diff(*args):
            if args and args[0] == "diff":
                return "??? malformed row"
            return real_jj(*args)

        with patch.object(repo, "jj", side_effect=malformed_diff):
            with self.assertRaisesRegex(integration.IntegrationError, "ambiguous diff summary"):
                repo.change(source)
        self.assertEqual(base, repo.resolve("integration"))
        self.assertEqual(source, repo.resolve("agent-c-staging"))

if __name__ == "__main__":
    unittest.main()
