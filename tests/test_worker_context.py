from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sfc_lib.common import Halt
from sfc_lib.worker_context import LANES, MAX_SKILL_BYTES, SCHEMAS, prepare


class WorkerContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sfc-worker-context-")
        self.addCleanup(self.temp.cleanup)
        self.package = Path(self.temp.name)
        self.scope = {"source": str(self.package), "head": "head", "base": "base",
                      "task": "task-digest", "config": "config-digest"}
        for lane in LANES:
            skill = self.package / "skills" / f"sfc-{lane}" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(f"# {lane}\nRead references/checks.md when required.\n")
        (self.package / "schemas").mkdir()
        for schema in SCHEMAS:
            (self.package / "schemas" / schema).write_text('{"type":"object"}\n')

    @property
    def skill(self):
        return self.package / "skills/sfc-qa/SKILL.md"

    def test_supplies_exact_skill_with_origin_scope_and_required_live_reads(self):
        context = prepare(self.package, "qa", self.scope)
        self.assertEqual(context.content, self.skill.read_bytes())
        self.assertIn("Read this repository's applicable AGENTS.md instructions", context.instructions)
        self.assertIn(str(self.skill.parent), context.instructions)
        self.assertIn(self.skill.read_text(), context.instructions)
        self.assertEqual(context.receipt()["skill_sha256"], hashlib.sha256(self.skill.read_bytes()).hexdigest())
        self.assertEqual(context.receipt()["scope"], self.scope)
        context.validate(self.scope)

    def test_release_qa_pins_only_the_dedicated_file_with_unchanged_receipt_shape(self):
        release = self.skill.with_name("RELEASE.md")
        release.write_text("# Release QA\nInspect the customer package.\n")
        source = prepare(self.package, "qa", self.scope)
        context = prepare(self.package, "qa", self.scope, release_qa=True)
        self.assertEqual(context.content, release.read_bytes())
        self.assertIn(str(release), context.instructions)
        self.assertIn("Read this repository's applicable AGENTS.md instructions", context.instructions)
        self.assertNotIn(self.skill.read_text(), context.instructions)
        self.assertEqual(context.receipt(), {**source.receipt(), "skill_path": str(release),
            "skill_sha256": hashlib.sha256(release.read_bytes()).hexdigest()})
        self.skill.write_text("Different source QA instructions")
        context.validate(self.scope)
        release.unlink()
        release.symlink_to(self.skill)
        with self.assertRaisesRegex(Halt, "changed before dispatch"):
            context.validate(self.scope)
        self.assertIsNone(prepare(self.package, "qa", self.scope, release_qa=True))
        self.assertEqual(prepare(self.package, "qa", self.scope).content, self.skill.read_bytes())
        with self.assertRaises(Halt):
            prepare(self.package, "review", self.scope, release_qa=True)

    def test_changed_skill_or_schema_invalidates_then_prepares_fresh(self):
        for path in (self.skill, *(self.package / "schemas" / name for name in SCHEMAS)):
            with self.subTest(path=path.name):
                context = prepare(self.package, "qa", self.scope)
                original = path.read_bytes()
                stamp = path.stat()
                path.write_bytes(original.replace(b"Read", b"Scan") if path == self.skill
                                 else original.replace(b"object", b"string"))
                os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                with self.assertRaisesRegex(Halt, "changed before dispatch"):
                    context.validate(self.scope)
                refreshed = prepare(self.package, "qa", self.scope)
                self.assertNotEqual(context.receipt(), refreshed.receipt())
                refreshed.validate(self.scope)
                path.write_bytes(original)

    def test_scope_is_immutable_and_every_binding_is_revalidated(self):
        context = prepare(self.package, "qa", self.scope)
        for key in self.scope:
            with self.subTest(key=key), self.assertRaisesRegex(Halt, "changed before dispatch"):
                context.validate({**self.scope, key: "changed"})
        receipt = context.receipt()
        receipt["scope"]["task"] = "edited returned receipt"
        self.assertEqual(context.receipt()["scope"]["task"], "task-digest")
        self.scope["config"] = "edited caller dictionary"
        with self.assertRaises(Halt):
            context.validate(self.scope)

    def test_unavailable_or_unsafe_package_falls_back_to_normal_discovery(self):
        original = self.skill.read_bytes()
        for invalid in (b"", b"\xff", b"# skill\0", b"x" * (MAX_SKILL_BYTES + 1)):
            with self.subTest(invalid=invalid[:8]):
                self.skill.write_bytes(invalid)
                self.assertIsNone(prepare(self.package, "qa", self.scope))
        self.skill.write_bytes(original)
        context = prepare(self.package, "qa", self.scope)
        self.skill.unlink()
        self.assertIsNone(prepare(self.package, "qa", self.scope))
        with self.assertRaises(Halt):
            context.validate(self.scope)

    def test_symlink_file_and_directory_are_never_inlined(self):
        target = self.package / "private.txt"
        target.write_text("private file must not become worker context\n")
        self.skill.unlink()
        self.skill.symlink_to(target)
        self.assertIsNone(prepare(self.package, "qa", self.scope))
        self.skill.unlink()
        directory = self.skill.parent
        directory.rmdir()
        directory.symlink_to(self.package / "skills/sfc-review", target_is_directory=True)
        self.assertIsNone(prepare(self.package, "qa", self.scope))

    def test_nonregular_file_never_blocks_or_becomes_context(self):
        self.skill.unlink()
        os.mkfifo(self.skill)
        self.assertIsNone(prepare(self.package, "qa", self.scope))

    def test_unsupported_nofollow_capability_keeps_normal_discovery(self):
        with patch("sfc_lib.worker_context.os.open", side_effect=NotImplementedError):
            self.assertIsNone(prepare(self.package, "qa", self.scope))
        flags = {name: value for name, value in vars(os).items() if name != "O_NOFOLLOW"}
        with patch.dict(vars(os), flags, clear=True):
            self.assertIsNone(prepare(self.package, "qa", self.scope))

    def test_parallel_preparation_keeps_each_role_and_task_independent(self):
        cases = [(lane, {**self.scope, "task": f"task-{number}"})
                 for number, lane in enumerate(LANES)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda case: prepare(self.package, *case), cases))
        for context, (lane, scope) in zip(results, cases):
            self.assertIn(f"# {lane}\n", context.instructions)
            self.assertEqual(context.receipt()["scope"], scope)
            context.validate(scope)

    def test_invalid_lane_or_incomplete_scope_never_prepares_context(self):
        for lane in ("../qa", "unknown"):
            with self.assertRaises(Halt):
                prepare(self.package, lane, self.scope)
        for scope in ({}, {**self.scope, "head": None}, {**self.scope, "source": {}}):
            with self.assertRaises(Halt):
                prepare(self.package, "qa", scope)


if __name__ == "__main__":
    unittest.main()
