"""Behavior tests; no network, chroot, or host package installation."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("recipe", HERE / "recipe.py")
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((HERE / "recipe.json").read_text())

    def test_default_is_candidate_not_release(self):
        plan = recipe.make_plan(self.config, Path("candidate"))
        self.assertFalse(plan["runtime_ready"])
        self.assertEqual(plan["architecture"], "arm64")
        self.assertEqual(plan["output_kind"], "raw-debian-base-not-installable")

    def test_all_sources_are_explicit_snapshots(self):
        plan = recipe.make_plan(self.config, Path("candidate"))
        sources = [x for x in plan["argv"] if x.startswith("deb ")]
        self.assertEqual(len(sources), 3)
        self.assertTrue(all("https://snapshot.debian.org/archive/" in x for x in sources))
        self.assertIn("bookworm-security", sources[2])
        self.assertNotIn("trusted=yes", " ".join(plan["argv"]))
        self.assertNotIn("AllowUnauthenticated", " ".join(plan["argv"]))
        self.assertIn("--mode=unshare", plan["argv"])
        self.assertIn("--skip=cleanup/apt/lists", plan["argv"])
        self.assertIn("--keyring=/usr/share/keyrings/debian-archive-keyring.gpg", plan["argv"])

    def test_reject_unsafe_or_floating_inputs(self):
        for key, value in [("snapshot", "latest"), ("snapshot", "20260230T000000Z"),
                           ("suite", "stable"), ("architecture", "amd64"),
                           ("packages", ["bash;touch /tmp/bad"]),
                           ("packages", self.config["packages"] + ["cron-"]),
                           ("packages", ["bash", "bash"]), ("packages", [])]:
            with self.subTest(key=key, value=value):
                bad = copy.deepcopy(self.config)
                bad[key] = value
                with self.assertRaises(ValueError):
                    recipe.make_plan(bad, Path("candidate"))

    def test_reject_unknown_and_boolean_schema(self):
        for change in [{"hook": "evil"}, {"schema_version": True}]:
            with self.assertRaises(ValueError):
                recipe.make_plan(dict(self.config, **change), Path("candidate"))

    def test_environment_is_fixed_and_does_not_inherit_apt_overrides(self):
        env = recipe.build_environment(self.config)
        self.assertNotIn("APT_CONFIG", env)
        self.assertEqual(env["LC_ALL"], "C.UTF-8")
        self.assertEqual(env["TZ"], "UTC")
        self.assertTrue(env["SOURCE_DATE_EPOCH"].isdigit())

    def test_preflight_rejects_windows_and_foreign_architecture(self):
        for system, machine in [("Windows", "ARM64"), ("Linux", "x86_64")]:
            with self.assertRaisesRegex(ValueError, "native Linux ARM64"):
                recipe.preflight(system=system, machine=machine)

    def test_existing_output_is_not_overwritten(self):
        with self.assertRaises(FileExistsError):
            recipe.prepare_output(HERE)

    def test_missing_package_requirements_rejected(self):
        bad = copy.deepcopy(self.config)
        bad["packages"].remove("python3")
        with self.assertRaisesRegex(ValueError, "required packages"):
            recipe.make_plan(bad, Path("candidate"))

    def test_build_preflight_failure_creates_nothing(self):
        with patch.object(recipe, "preflight", side_effect=ValueError("blocked")), \
                patch.object(recipe, "prepare_output") as prepare, \
                patch.object(recipe.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "blocked"):
                recipe.build(self.config, Path("candidate"))
            prepare.assert_not_called()
            run.assert_not_called()

    def test_planning_never_runs_commands_or_creates_output(self):
        with patch.object(recipe, "prepare_output") as prepare, \
                patch.object(recipe.subprocess, "run") as run:
            recipe.make_plan(self.config, Path("candidate with spaces"))
            prepare.assert_not_called()
            run.assert_not_called()

    def test_shell_hook_explicitly_receives_chroot_path(self):
        hook = next(x for x in recipe.make_plan(self.config, Path("candidate"))["argv"]
                    if x.startswith("--customize-hook="))
        self.assertTrue(hook.endswith(' "$1"'))

    def test_namespace_uses_stdout_not_private_output_path(self):
        plan = recipe.make_plan(self.config, Path("private candidate"))
        i = plan["argv"].index("bookworm")
        self.assertEqual(plan["argv"][i + 1], "-")
        self.assertNotIn(str(Path("private candidate").absolute()), " ".join(plan["argv"]))
        hook = next(x for x in plan["argv"] if x.startswith("--customize-hook="))
        self.assertNotIn(str(HERE), hook)


if __name__ == "__main__":
    unittest.main()
