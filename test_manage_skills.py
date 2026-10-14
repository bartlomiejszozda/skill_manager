"""Filesystem tests for assignment and ownership behavior; no live installs."""

import json
from argparse import Namespace
from contextlib import redirect_stdout
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import manage_skills


SCRIPT = Path(__file__).with_name("manage_skills.py")


class SkillLinksTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "skills_source").mkdir()
        self.config = {"targets": {"one": "repo-one", "two": "repo-two"}, "collections": {}}

    def skill(self, collection="personal", name="example", nested=False):
        directory = self.root / "skills_source" / collection
        if nested:
            directory /= ".agents/skills"
        directory /= name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Example\n---\n")
        self.config["collections"].setdefault(collection, {"targets": ["one"]})
        return directory

    def run_command(self, command="apply", success=True, save=True, answers=None):
        config_path = self.root / "skill-assignments.yaml"
        if save:
            config_path.write_text(json.dumps(self.config))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, "--config", str(config_path)],
            capture_output=True, text=True, cwd="/tmp", input=answers,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def state(self):
        return json.loads((self.root / "skill-links-state.json").read_text())["links"]

    def test_defaults_overrides_and_nowhere(self):
        source = self.skill(name="default")
        self.skill(name="override")
        self.skill(name="excluded")
        self.config["collections"]["personal"].update({
            "targets": ["one", "two"],
            "skills": {"override": ["two"], "excluded": []},
        })
        self.run_command()
        for target in ("repo-one", "repo-two"):
            link = self.root / target / "default"
            self.assertEqual(link.resolve(), source)
            self.assertFalse(Path(link.readlink()).is_absolute())
            self.assertFalse((self.root / target / "excluded").exists())
        self.assertFalse((self.root / "repo-one/override").exists())
        self.assertTrue((self.root / "repo-two/override").is_symlink())
        self.assertEqual(len(self.state()), 3)
        self.assertIn("0 changes", self.run_command("plan").stdout)

    def test_detects_nested_and_symlinked_packages(self):
        source = self.skill("vendor", nested=True)
        other = self.skill("personal", "other")
        alias = source.parent / "alias"
        alias.symlink_to(other, target_is_directory=True)
        self.run_command()
        self.assertEqual((self.root / "repo-one/alias").resolve(), other)

    def test_check_and_plan_are_read_only(self):
        self.skill()
        for command in ("check", "plan"):
            self.run_command(command)
            self.assertFalse((self.root / "repo-one").exists())
            self.assertFalse((self.root / "skill-links-state.json").exists())
            self.assertFalse((self.root / ".skill-links.lock").exists())

    def test_existing_file_directory_or_symlink_is_a_collision(self):
        source = self.skill()
        destination = self.root / "repo-one/example"
        destination.parent.mkdir()
        for kind in ("file", "directory", "same-link", "dangling-link"):
            with self.subTest(kind=kind):
                if kind == "file":
                    destination.write_text("personal content")
                elif kind == "directory":
                    destination.mkdir()
                else:
                    destination.symlink_to(source if kind == "same-link" else self.root / "missing")
                result = self.run_command(success=False)
                self.assertIn("Name already exists", result.stderr)
                self.assertFalse((self.root / "skill-links-state.json").exists())
                if kind == "directory":
                    destination.rmdir()
                else:
                    destination.unlink()

    def test_duplicate_names_at_one_target_fail_before_creation(self):
        self.skill("first")
        self.skill("second")
        self.assertIn("Skill name collision", self.run_command(success=False).stderr)
        self.assertFalse((self.root / "repo-one").exists())

    def test_removed_and_renamed_skills_clean_up_links(self):
        source = self.skill()
        self.run_command()
        source.rename(source.with_name("renamed"))
        self.run_command()
        self.assertFalse((self.root / "repo-one/example").is_symlink())
        self.assertTrue((self.root / "repo-one/renamed").is_symlink())
        shutil.rmtree(source.with_name("renamed"))
        self.run_command()
        self.assertFalse((self.root / "repo-one/renamed").is_symlink())
        self.assertEqual(self.state(), [])

    def test_assignment_changes_remove_old_links(self):
        self.skill()
        self.run_command()
        self.config["collections"]["personal"]["targets"] = ["two"]
        del self.config["targets"]["one"]
        self.run_command()
        self.assertFalse((self.root / "repo-one/example").is_symlink())
        self.assertTrue((self.root / "repo-two/example").is_symlink())
        self.config["collections"]["personal"]["targets"] = []
        self.run_command()
        self.assertEqual(self.state(), [])

    def test_source_location_change_replaces_managed_link(self):
        self.skill()
        self.run_command()
        (self.root / "skills_source/personal").rename(self.root / "skills_source/renamed")
        self.config["collections"]["renamed"] = self.config["collections"].pop("personal")
        self.assertIn("REPLACE", self.run_command("plan").stdout)
        self.run_command()
        self.assertEqual((self.root / "repo-one/example").resolve(), self.root / "skills_source/renamed/example")

    def test_modified_managed_links_block_all_mutations(self):
        self.skill()
        self.run_command()
        destination = self.root / "repo-one/example"
        destination.unlink()
        destination.symlink_to(self.root / "personal")
        self.skill(name="new")
        result = self.run_command(success=False)
        self.assertIn("Managed link was changed", result.stderr)
        self.assertEqual(destination.readlink(), self.root / "personal")
        self.assertFalse((self.root / "repo-one/new").exists())

    def test_deleted_managed_link_is_recreated(self):
        self.skill()
        self.run_command()
        (self.root / "repo-one/example").unlink()
        self.run_command()
        self.assertTrue((self.root / "repo-one/example").is_symlink())

    def test_missing_unassigned_link_is_forgotten(self):
        self.skill()
        self.run_command()
        (self.root / "repo-one/example").unlink()
        self.config["collections"]["personal"]["targets"] = []
        self.assertIn("FORGET", self.run_command("plan").stdout)
        self.run_command()
        self.assertEqual(self.state(), [])

    def test_broken_source_symlink_cleans_up_managed_link(self):
        source = self.skill("vendor", nested=True)
        actual = self.root / "source-package"
        source.rename(actual)
        source.symlink_to(actual, target_is_directory=True)
        self.run_command()
        shutil.rmtree(actual)
        self.run_command()
        self.assertFalse((self.root / "repo-one/example").is_symlink())

    def test_new_skills_inherit_without_config_edits(self):
        self.skill()
        self.run_command()
        self.skill(name="new")
        self.run_command()
        self.assertTrue((self.root / "repo-one/new").is_symlink())

    def test_unknown_targets_and_stale_overrides_fail(self):
        self.skill()
        self.config["collections"]["personal"]["targets"] = ["typo"]
        self.assertIn("Unknown targets", self.run_command("check", success=False).stderr)
        self.config["collections"]["personal"].update({"targets": [], "skills": {"removed": []}})
        self.assertIn("Configured skills not found", self.run_command("check", success=False).stderr)

    def test_removed_collection_can_be_cleaned_up(self):
        self.skill()
        self.run_command()
        shutil.rmtree(self.root / "skills_source/personal")
        del self.config["collections"]["personal"]
        self.run_command()
        self.assertEqual(self.state(), [])

    def test_duplicate_yaml_keys_fail(self):
        (self.root / "skill-assignments.yaml").write_text("targets: {}\ntargets: {}\ncollections: {}\n")
        self.assertIn("duplicate", self.run_command("check", success=False, save=False).stderr)

    def test_target_aliases_fail(self):
        self.skill()
        self.config["targets"]["two"] = "repo-one"
        self.assertIn("same directory", self.run_command("check", success=False).stderr)

    def test_relative_links_work_through_symlinked_workspace(self):
        source = self.skill()
        with tempfile.TemporaryDirectory() as physical:
            (self.root / "alias").symlink_to(physical, target_is_directory=True)
            self.config["targets"]["one"] = "alias/.agents/skills"
            self.run_command()
            self.assertEqual((self.root / "alias/.agents/skills/example").resolve(), source)
            self.assertIn("0 changes", self.run_command("plan").stdout)
            self.config["collections"]["personal"]["targets"] = []
            self.run_command()
            self.assertFalse((Path(physical) / ".agents/skills/example").is_symlink())

    def test_dangling_target_ancestor_blocks_cleanup(self):
        source = self.skill()
        self.run_command()
        before = self.state()
        (self.root / "repo-two").mkdir()
        (self.root / "repo-two/.agents").symlink_to(self.root / "missing")
        self.config["targets"]["two"] = "repo-two/.agents/skills"
        self.config["collections"]["personal"]["targets"] = ["two"]
        self.assertIn("invalid parent directory", self.run_command(success=False).stderr)
        self.assertEqual((self.root / "repo-one/example").resolve(), source)
        self.assertEqual(self.state(), before)

    def test_state_paths_are_project_relative(self):
        self.skill()
        self.run_command()
        self.assertEqual(self.state(), [{"destination": "repo-one/example", "source": "skills_source/personal/example"}])

    def test_sync_requires_both_approvals(self):
        self.skill()
        for answers in ("n\n", "\n", "yes\nno\n", "yes\n\n", "", "yes\n"):
            with self.subTest(answers=answers):
                result = self.run_command("sync", answers=answers)
                self.assertIn("Cancelled", result.stdout)
                self.assertFalse((self.root / "repo-one").exists())
                self.assertFalse((self.root / "skill-links-state.json").exists())
                self.assertFalse((self.root / ".skill-links.lock").exists())

    def test_sync_shows_plan_only_after_first_approval(self):
        self.skill()
        self.assertNotIn("CREATE", self.run_command("sync", answers="no\n").stdout)
        self.assertIn("CREATE", self.run_command("sync", answers="yes\nno\n").stdout)

    def test_sync_applies_approved_plan_and_then_is_up_to_date(self):
        source = self.skill()
        result = self.run_command("sync", answers="yes\nyes\n")
        self.assertIn("Changes applied", result.stdout)
        self.assertEqual((self.root / "repo-one/example").resolve(), source)
        result = self.run_command("sync", answers="")
        self.assertIn("Everything is up to date", result.stdout)
        self.assertNotIn("[y/N]", result.stdout)

    def test_sync_reprompts_for_invalid_answer(self):
        self.skill()
        result = self.run_command("sync", answers="maybe\nyes\nno\n")
        self.assertIn("Please enter yes or no", result.stdout)
        self.assertFalse((self.root / "repo-one").exists())

    def test_sync_checks_collisions_before_prompting(self):
        self.skill()
        (self.root / "repo-one").mkdir()
        (self.root / "repo-one/example").write_text("existing")
        result = self.run_command("sync", success=False, answers="yes\nyes\n")
        self.assertIn("Name already exists", result.stderr)
        self.assertNotIn("[y/N]", result.stdout)

    def test_sync_refuses_a_plan_changed_during_review(self):
        self.skill()
        self.run_command("check")
        config_path = self.root / "skill-assignments.yaml"
        def approve(question):
            if question.startswith("Apply"):
                self.config["collections"]["personal"]["targets"] = ["two"]
                config_path.write_text(json.dumps(self.config))
            return True

        with patch("manage_skills.confirm", side_effect=approve), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(manage_skills.ConfigurationError, "plan changed"):
                manage_skills.run(Namespace(command="sync", config=config_path))
        self.assertFalse((self.root / "repo-one").exists())
        self.assertFalse((self.root / "repo-two").exists())
        self.assertFalse((self.root / "skill-links-state.json").exists())

    def test_sync_refuses_retargeted_workspace_during_review(self):
        self.skill()
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(first, target_is_directory=True)
        self.config["targets"]["one"] = "alias/.agents/skills"
        self.run_command("check")

        def approve(question):
            if question.startswith("Apply"):
                alias.unlink()
                alias.symlink_to(second, target_is_directory=True)
            return True

        with patch("manage_skills.confirm", side_effect=approve), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(manage_skills.ConfigurationError, "plan changed"):
                manage_skills.run(Namespace(command="sync", config=self.root / "skill-assignments.yaml"))
        self.assertFalse((first / ".agents").exists())
        self.assertFalse((second / ".agents").exists())
        self.assertFalse((self.root / "skill-links-state.json").exists())


if __name__ == "__main__":
    unittest.main()
