"""Updater Git integration against temporary local repositories.

Native Params, logging, hardware, and consistency syncing are explicit test doubles.
Git fetch/checkout/reset/clean and finalization copies run against real temporary
repositories. No device paths, mounts, firmware calls, or network remotes are used.
"""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch


from openpilot.common.vendor_manifest import DEPENDENCIES

BRANCH = "test-vendored"


class MemoryParams:
  def __init__(self):
    self.values = {"UpdaterTargetBranch": BRANCH}

  def get(self, key, **kwargs):
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.values.get(key, False))

  def remove(self, key):
    self.values.pop(key, None)

  def put(self, key, value, **kwargs):
    self.values[key] = value

  put_bool = put


def load_updater():
  """Load unchanged updater code with explicit service boundaries substituted."""
  services = {
    "openpilot.common.params": {"Params": MemoryParams},
    "openpilot.common.time_helpers": {"system_time_valid": Mock()},
    "openpilot.common.markdown": {"parse_markdown": Mock()},
    "openpilot.common.swaglog": {"cloudlog": Mock()},
    "openpilot.selfdrive.selfdrived.alertmanager": {"set_offroad_alert": Mock()},
    "openpilot.common.hardware": {"AGNOS": False, "HARDWARE": Mock()},
    "openpilot.starpilot.drive_state.evidence": {"PhysicalSource": Mock()},
    "openpilot.common.version": {"get_build_metadata": Mock()},
  }
  modules = {}
  for name, attributes in services.items():
    module = ModuleType(name)
    module.__dict__.update(attributes)
    modules[name] = module
  path = Path(__file__).resolve().parents[1] / "updated.py"
  spec = importlib.util.spec_from_file_location("updater_git_test", path)
  assert spec is not None and spec.loader is not None
  updated = importlib.util.module_from_spec(spec)
  with patch.dict(sys.modules, modules):
    spec.loader.exec_module(updated)
  return updated


class TestVendoredUpdate(unittest.TestCase):
  def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.addCleanup(self.temporary.cleanup)
    self.root = Path(self.temporary.name)
    self.source = self.root / "source"
    self.merged = self.root / "merged"
    self.finalized = self.root / "finalized"
    self.source.mkdir()
    self.env_patch = patch.dict(os.environ, {
      "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
      "GIT_AUTHOR_NAME": "Updater test", "GIT_COMMITTER_NAME": "Updater test",
      "GIT_AUTHOR_EMAIL": "updater@example.invalid", "GIT_COMMITTER_EMAIL": "updater@example.invalid",
    })
    self.env_patch.start()
    self.addCleanup(self.env_patch.stop)
    self.git(self.source, "init", "-b", BRANCH)
    self.git(self.source, "config", "uploadpack.allowFilter", "true")
    for name in DEPENDENCIES:
      folder = self.source / name
      folder.mkdir()
      (folder / "README.md").write_text(f"{name} tracked source\n")
    self.manifest = {
      "schema_version": 1,
      "upstream": {"url": "https://github.com/commaai/openpilot.git", "commit": "1" * 40},
      "dependencies": [
        {"path": name, "url": f"https://github.com/commaai/{name}.git", "commit": "2" * 40, "tree": "3" * 40, "exclude": []}
        for name in DEPENDENCIES
      ],
    }
    self.write_manifest()
    self.initial_commit = self.commit_source("Initial vendored tree")
    self.git(self.root, "clone", "--no-local", "--no-hardlinks", str(self.source), str(self.merged))
    # Simulate inherited Git settings that would otherwise recurse on fetch/reset.
    self.git(self.merged, "config", "submodule.recurse", "true")
    self.git(self.merged, "config", "fetch.recurseSubmodules", "true")

    self.updated = load_updater()
    self.updated.STAGING_ROOT = str(self.root)
    self.updated.OVERLAY_MERGED = str(self.merged)
    self.updated.FINALIZED = str(self.finalized)
    self.updated.BASEDIR = str(self.merged)
    self.updated.set_consistent_flag = self.set_consistent_flag
    self.updated.handle_agnos_update = Mock()
    self.commands = []
    real_run = self.updated.run

    def bounded_run(command, cwd=None):
      if cwd is None:
        raise AssertionError("Every updater command must name its isolated working directory")
      self.assertTrue(Path(cwd).resolve().is_relative_to(self.root.resolve()))
      self.assertNotEqual(command[:2], ["git", "submodule"])
      self.commands.append(command)
      if command in (["git", "gc"], ["git", "lfs", "prune"]):
        return ""
      return real_run(command, cwd)

    self.updated.run = bounded_run
    self.updater = self.updated.Updater()

  @staticmethod
  def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT, text=True).strip()

  def write_manifest(self):
    (self.source / "upstream-sync.json").write_text(json.dumps(self.manifest))

  def commit_source(self, message):
    self.git(self.source, "add", "--all")
    self.git(self.source, "commit", "-m", message)
    return self.git(self.source, "rev-parse", "HEAD")

  def set_consistent_flag(self, consistent):
    marker = self.finalized / ".overlay_consistent"
    if consistent:
      marker.touch()
    else:
      marker.unlink(missing_ok=True)

  def assert_rejected_before_checkout(self):
    self.updated.AGNOS = True
    with self.assertRaises(ValueError):
      self.updater.fetch_update()
    self.assertEqual(self.git(self.merged, "rev-parse", "HEAD"), self.initial_commit)
    self.assertFalse(any(command[:2] == ["git", "checkout"] for command in self.commands))
    self.assertFalse((self.finalized / ".overlay_consistent").exists())
    self.updated.handle_agnos_update.assert_not_called()
    self.assertFalse(self.updater.params.values["UpdateAvailable"])

  def test_automatic_update_uses_fast_path_and_only_falls_back_on_incompatibility(self):
    fast_update = importlib.import_module("openpilot.starpilot.software.fast_update")
    for error in (None, self.updated.FastUpdateIncompatible("AGNOS changed"), self.updated.FastUpdateError("Tracked edits")):
      with self.subTest(error=error), patch.object(fast_update, "fast_update", side_effect=error, return_value="reboot-requested"), \
           patch.object(self.updater, "normal_update", return_value="reboot-requested") as normal:
        if error is not None and not isinstance(error, self.updated.FastUpdateIncompatible):
          with self.assertRaises(self.updated.FastUpdateError):
            self.updater.fast_update(BRANCH, self.initial_commit, automatic=True)
        else:
          self.assertEqual(self.updater.fast_update(BRANCH, self.initial_commit, automatic=True), "reboot-requested")
        self.assertEqual(normal.call_count, int(isinstance(error, self.updated.FastUpdateIncompatible)))
    with patch.object(fast_update, "fast_update", side_effect=self.updated.FastUpdateIncompatible("AGNOS changed")), \
         patch.object(self.updater, "normal_update") as normal:
      with self.assertRaises(self.updated.FastUpdateIncompatible):
        self.updater.fast_update(BRANCH, self.initial_commit)
      normal.assert_not_called()  # Explicit fast/rollback/version operations keep their contract.

  def test_normal_fallback_stages_flashes_and_requests_reboot_with_fresh_parked_state(self):
    installed = self.root / "installed"
    self.git(self.root, "clone", "--no-local", str(self.merged), str(installed))
    self.updated.BASEDIR = str(installed)
    (self.source / "panda/README.md").write_text("New native revision")
    target = self.commit_source("Full update")
    self.updated.AGNOS = True
    progress = Mock()
    with patch.object(self.updated, "init_overlay"):
      self.assertEqual(self.updater.normal_update(BRANCH, self.initial_commit, lambda: True, progress), "reboot-requested")
    self.updated.handle_agnos_update.assert_called_once()
    self.assertEqual(self.git(installed, "rev-parse", "HEAD"), self.initial_commit)
    self.assertEqual(self.git(self.finalized, "rev-parse", "HEAD"), target)
    self.assertTrue(self.updater.params.values["DoReboot"])
    self.assertTrue(self.updater.params.values["UpdateAvailable"])
    progress.assert_called_with("complete", "Update installed. Restarting device.")

  def test_normal_completion_survives_recovery_before_reboot(self):
    installed = self.root / "installed"
    self.git(self.root, "clone", "--no-local", str(self.merged), str(installed))
    self.updated.BASEDIR = str(installed)
    (self.source / "panda/README.md").write_text("New native revision")
    self.commit_source("Full update")
    writes = []

    def progress(stage, detail):
      self.updater.params.put("UpdaterFastState", {"version": 1, "stage": stage, "detail": detail, "branch": BRANCH})
      writes.append(stage)

    def request_reboot(key, value, **kwargs):
      if key == "DoReboot":
        self.assertEqual(self.updater.params.values["UpdaterFastState"]["stage"], "complete")
      self.updater.params.put(key, value, **kwargs)

    with patch.object(self.updated, "init_overlay"), patch.object(self.updater.params, "put_bool", side_effect=request_reboot):
      self.assertEqual(self.updater.normal_update(BRANCH, self.initial_commit, lambda: True, progress), "reboot-requested")
    self.assertEqual(writes[-1], "complete")
    completed = dict(self.updater.params.values["UpdaterFastState"])
    self.updated.recover_fast_state(self.updater.params)
    self.assertEqual(self.updater.params.values["UpdaterFastState"], completed)
    self.assertTrue(self.updater.params.values["DoReboot"])

  def test_normal_fallback_never_reboots_after_parked_authority_is_lost(self):
    installed = self.root / "installed"
    self.git(self.root, "clone", "--no-local", str(self.merged), str(installed))
    self.updated.BASEDIR = str(installed)
    (self.source / "panda/README.md").write_text("New native revision")
    self.commit_source("Full update")
    parked = iter((True, False))
    with patch.object(self.updated, "init_overlay"), self.assertRaises(self.updated.FastUpdateError):
      self.updater.normal_update(BRANCH, self.initial_commit, lambda: next(parked), Mock())
    self.assertNotIn("DoReboot", self.updater.params.values)

  def test_check_reports_confirmed_os_requirement_without_changing_installed_files(self):
    self.updated.AGNOS = True
    self.updated.HARDWARE.get_os_version.return_value = "12"
    for launch, required in (("export AGNOS_VERSION='12'\n", False), ("export AGNOS_VERSION='13'\n", True),
                             ("# Unknown OS version\n", None)):
      with self.subTest(launch=launch):
        (self.source / "launch_env.sh").write_text(launch)
        self.commit_source("Change target OS version")
        self.updater.check_for_update()
        expected = {} if required is None else {"branch": BRANCH, "required": required}
        self.assertEqual(self.updater.params.values["UpdaterOSUpdate"], expected)
        self.assertEqual(self.git(self.merged, "rev-parse", "HEAD"), self.initial_commit)
        self.updated.handle_agnos_update.assert_not_called()

  def test_vendored_fetch_and_finalize(self):
    (self.source / "panda" / "README.md").write_text("Updated ordinary tracked source\n")
    target = self.commit_source("Update dependency source atomically")
    self.updater.fetch_update()
    self.assertEqual(self.git(self.merged, "rev-parse", "HEAD"), target)
    self.assertEqual(self.git(self.finalized, "rev-parse", "HEAD"), target)
    self.assertEqual((self.finalized / "panda" / "README.md").read_text(), "Updated ordinary tracked source\n")
    self.assertTrue((self.finalized / ".overlay_consistent").is_file())
    for name in DEPENDENCIES:
      self.assertFalse((self.finalized / name / ".git").exists())
    self.assertFalse(any(line.startswith("160000 ") for line in self.git(self.finalized, "ls-tree", "-r", "HEAD").splitlines()))
    self.assertEqual(self.git(self.merged, "config", "submodule.recurse"), "false")
    self.assertEqual(self.git(self.merged, "config", "fetch.recurseSubmodules"), "false")
    self.updated.handle_agnos_update.assert_not_called()

  def prepare_agnos_target(self):
    self.updated.AGNOS = True
    self.updated.HARDWARE.get_os_version.return_value = "12"
    (self.source / "launch_env.sh").write_text("export AGNOS_VERSION='13'\n")
    (self.source / "panda/README.md").write_text("New native revision\n")
    return self.commit_source("New AGNOS target")

  def assert_no_partial_clone(self, repo):
    for key in ("remote.origin.promisor", "remote.origin.partialclonefilter"):
      self.assertEqual(self.git(repo, "config", "--get", "--default=", key), "")
    self.assertFalse((repo / ".git/shallow").exists())

  def test_agnos_peek_does_not_change_installed_or_overlay_clone(self):
    installed = self.root / "installed"
    self.git(self.root, "clone", "--no-local", str(self.merged), str(installed))
    self.updated.BASEDIR = str(installed)
    self.prepare_agnos_target()
    self.updater.check_for_update()
    self.assertEqual(self.updater.params.values["UpdaterOSUpdate"], {"branch": BRANCH, "required": True})
    for repo in (installed, self.merged):
      self.assertEqual(self.git(repo, "rev-parse", "HEAD"), self.initial_commit)
      self.assert_no_partial_clone(repo)
    self.assertFalse(any(path.name.startswith("agnos-version-") for path in self.root.iterdir()))

  def test_agnos_check_then_download_has_all_blobs_before_checkout(self):
    target = self.prepare_agnos_target()
    self.updater.check_for_update()
    fetch_missing = []
    original = self.updated.run

    def observe(command, cwd=None):
      result = original(command, cwd)
      if command[0] == "git" and "fetch" in command and cwd == str(self.merged):
        fetch_missing.append([line for line in self.git(self.merged, "rev-list", "--objects", "--missing=print", "FETCH_HEAD").splitlines()
                              if line.startswith("?")])
      return result

    self.updated.run = observe
    self.assertTrue(self.updater.fetch_update())
    self.assertEqual(fetch_missing, [[]])
    self.assertEqual(self.git(self.finalized, "rev-parse", "HEAD"), target)
    self.assert_no_partial_clone(self.finalized)

  def test_download_fills_blobs_missing_from_previously_filtered_fetch(self):
    target = self.prepare_agnos_target()
    # Reproduce a repository already contaminated by the old AGNOS peek.
    self.git(self.merged, "fetch", "--depth=1", "--filter=blob:none", "origin", BRANCH)
    self.assertIn("?", self.git(self.merged, "rev-list", "--objects", "--missing=print", "FETCH_HEAD"))
    self.updater.check_for_update()
    original = self.updated.run
    fetch_missing = []

    def observe(command, cwd=None):
      result = original(command, cwd)
      if command[0] == "git" and "fetch" in command and cwd == str(self.merged):
        self.assertNotIn("--refetch", command)
        fetch_missing.append([line for line in self.git(self.merged, "rev-list", "--objects", "--missing=print", f"{target}^{{tree}}").splitlines()
                              if line.startswith("?")])
      if command[:2] == ["git", "rev-parse"] and command[-1] == "FETCH_HEAD^{commit}":
        self.assertEqual(result.strip(), target)
      if command[:2] == ["git", "rev-list"] and "--missing=print" in command:
        self.assertIn(f"{target}^{{tree}}", command)
      if command[:2] == ["git", "checkout"]:
        self.assertEqual(fetch_missing[-1], [])
      return result

    self.updated.run = observe
    original_validate = self.updated.validate_revision

    def offline_validate(repo, revision):
      self.assertIn(revision, (target, "HEAD"))
      self.assertEqual(fetch_missing[-1], [])
      self.git(self.merged, "remote", "set-url", "origin", str(self.root / "unavailable"))
      return original_validate(repo, revision)

    self.updated.validate_revision = offline_validate
    self.assertTrue(self.updater.fetch_update())
    self.assertEqual(len(fetch_missing), 2)
    self.assertTrue(fetch_missing[0])
    self.assertEqual(fetch_missing[-1], [])
    self.assertEqual(self.git(self.finalized, "rev-parse", "HEAD"), target)

  def test_filtered_new_and_known_targets_fetch_incrementally(self):
    (self.source / "model.bin").write_bytes(os.urandom(256_000))
    self.commit_source("Large stable model")
    counter = self.root / "transfer"
    upload_pack = self.root / "upload-pack-counter"
    upload_pack.write_text('#!/bin/sh\ngit-upload-pack "$@" | tee -a ' + shlex.quote(str(counter)) + '\n')
    upload_pack.chmod(0o755)
    self.git(self.merged, "config", "remote.origin.uploadpack", str(upload_pack))
    self.assertTrue(self.updater.fetch_update())
    stable_cost = counter.stat().st_size
    self.assertGreater(stable_cost, 256_000)
    (self.source / "peeked.txt").write_text("Peeked snapshot, never installed\n")
    self.commit_source("Peeked software update")
    self.git(self.merged, "fetch", "--depth=1", "--filter=blob:none", "origin", BRANCH)
    original = self.updated.run
    fetched = []
    missing = []
    target = None

    def observe(command, cwd=None):
      result = original(command, cwd)
      if command[0] == "git" and "fetch" in command and cwd == str(self.merged):
        fetched.append(command)
        missing.append([row for row in self.git(self.merged, "rev-list", "--objects", "--missing=print", f"{target}^{{tree}}").splitlines()
                        if row.startswith("?")])
      return result

    self.updated.run = observe
    original_validate = self.updated.validate_revision

    def offline_validate(repo, revision):
      self.assertEqual(missing[-1], [])
      self.git(self.merged, "remote", "set-url", "origin", str(self.root / "unavailable"))
      return original_validate(repo, revision)

    self.updated.validate_revision = offline_validate
    filters = []
    transferred = []
    for update in range(2):
      (self.source / "incremental.txt").write_text(f"Small update {update}\n")
      target = self.commit_source("Incremental software update")
      # First install a new target with a peeked parent, then a known target.
      if update == 1:
        self.git(self.merged, "fetch", "--depth=1", "--filter=blob:none", "origin", BRANCH)
        self.assertIn("?", self.git(self.merged, "rev-list", "--objects", "--missing=print", f"{target}^{{tree}}"))
      self.updater.check_for_update()
      counter.write_bytes(b"")
      try:
        self.assertTrue(self.updater.fetch_update())
      finally:
        self.git(self.merged, "remote", "set-url", "origin", str(self.source))
      self.assertEqual(self.git(self.finalized, "rev-parse", "HEAD"), target)
      filters.append(self.git(self.finalized, "config", "--get", "--default=", "remote.origin.partialclonefilter"))
      self.assertEqual(self.git(self.finalized, "config", "remote.origin.promisor"), "true")
      transferred.append(counter.stat().st_size)
    print(f'normal transfer bytes: stable={stable_cost}, tiny updates={transferred}')
    self.assertEqual(filters, ["", ""])
    self.assertFalse(any("--refetch" in command for command in fetched))
    self.assertEqual(len(fetched), 4)
    self.assertTrue(missing[0])
    self.assertTrue(missing[2])
    self.assertEqual(missing[1::2], [[], []])
    self.assertTrue(all(size < stable_cost // 10 for size in transferred), (stable_cost, transferred))

  def test_snapshot_fill_obeys_phone_uplink_guard_before_checkout(self):
    target = self.prepare_agnos_target()
    self.git(self.merged, "fetch", "--depth=1", "--filter=blob:none", "origin", BRANCH)
    self.git(self.merged, "config", "--unset", "remote.origin.partialclonefilter")
    self.updater.check_for_update()
    original = self.updated.run
    blocked = False
    fetched = []

    def observe(command, cwd=None):
      nonlocal blocked
      if "fetch" in command:
        fetched.append(command)
      result = original(command, cwd)
      if "fetch" in command:
        blocked = True
      return result

    self.updated.run = observe
    with patch.dict(self.updated.run_git.__globals__, {"phone_uplink_active": lambda: blocked}), \
         patch.object(self.updated, "validate_revision") as validate:
      with self.assertRaisesRegex(self.updated.FastUpdateError, "phone uplink"):
        self.updater.fetch_update()
    self.assertEqual(len(fetched), 2)
    self.assertIn("fetch.negotiationAlgorithm=noop", fetched[-1])
    validate.assert_not_called()
    self.assertFalse(any(command[:2] == ["git", "checkout"] for command in self.commands))
    self.assertEqual(self.git(self.merged, "rev-parse", "HEAD"), self.initial_commit)
    self.assertIn("?", self.git(self.merged, "rev-list", "--objects", "--missing=print", f"{target}^{{tree}}"))
    self.assertFalse((self.finalized / ".overlay_consistent").exists())

  def test_phone_uplink_check_loop_defers_without_failures_or_overlay_invalidation(self):
    target = self.prepare_agnos_target()
    self.updated.LOCK_FILE = str(self.root / "lock")
    self.updated.OVERLAY_INIT = self.root / ".overlay_init"
    self.updated.OVERLAY_INIT.touch()
    self.updater.params.put("InstallDate", "test")
    self.updater.params.put("UpdateFailedCount", 0)
    helper = self.updated.WaitTimeHelper.__new__(self.updated.WaitTimeHelper)
    helper.ready_event = threading.Event()
    helper.request_lock = threading.RLock()
    helper.request_generation = 0
    helper.fast_target = None
    helper.user_request = self.updated.UserRequest.NONE
    helper.sleep = Mock(side_effect=[None, None, StopIteration])
    # Keep the real check and loop; replace mounting, service lifetime and unrelated status rendering.
    with patch.object(self.updated, "Params", return_value=self.updater.params), \
         patch.object(self.updated, "Updater", return_value=self.updater), \
         patch.object(self.updated, "WaitTimeHelper", return_value=helper), \
         patch.object(self.updated, "init_overlay") as init, \
         patch.object(self.updated, "system_time_valid", return_value=True), \
         patch.object(self.updater, "set_params") as status, \
         patch.object(self.updater, "fetch_update") as fetch, \
         patch.object(self.updated, "phone_uplink_active", return_value=True), \
         patch.dict(self.updated.run_git.__globals__, {"phone_uplink_active": Mock(return_value=True)}):
      with self.assertRaises(StopIteration):
        self.updated.main()
    self.assertEqual(helper.sleep.call_args_list, [unittest.mock.call(60), unittest.mock.call(5400), unittest.mock.call(5400)])
    self.assertEqual(init.call_count, 3)
    self.assertTrue(self.updated.OVERLAY_INIT.exists())
    self.assertTrue(all(call.args == (False, 0, None) or call.args == (True, 0, None) for call in status.call_args_list))
    fetch.assert_not_called()
    self.assertEqual(self.updater.branches[BRANCH], target)
    self.assertTrue(self.updater.has_internet)
    self.assertEqual(self.updater.params.values["UpdaterOSUpdate"], {})
    self.assertFalse(any(command[0] == "git" and "fetch" in command for command in self.commands))

  def test_phone_uplink_activation_during_peek_does_not_fail_check(self):
    self.prepare_agnos_target()
    with patch.object(self.updated, "phone_uplink_active", return_value=False), \
         patch.dict(self.updated.run_git.__globals__, {"phone_uplink_active": Mock(return_value=True)}):
      self.updater.check_for_update()
    self.assertTrue(self.updater.has_internet)
    self.assertEqual(self.updater.params.values["UpdaterOSUpdate"], {})
    self.assertFalse(any(path.name.startswith("agnos-version-") for path in self.root.iterdir()))

  def test_invalid_manifest_rejected_before_checkout(self):
    self.manifest["schema_version"] = 999
    self.write_manifest()
    self.commit_source("Unsupported manifest")
    self.assert_rejected_before_checkout()

  def test_missing_manifest_rejected_before_checkout(self):
    (self.source / "upstream-sync.json").unlink()
    self.commit_source("Missing manifest")
    self.assert_rejected_before_checkout()

  def test_malformed_manifest_rejected_before_checkout(self):
    (self.source / "upstream-sync.json").write_text("{invalid JSON")
    self.commit_source("Malformed manifest")
    self.assert_rejected_before_checkout()

  def test_missing_dependency_rejected_before_checkout(self):
    shutil.rmtree(self.source / "panda")
    self.commit_source("Missing ordinary dependency folder")
    self.assert_rejected_before_checkout()

  def test_gitlink_rejected_before_checkout(self):
    self.git(self.source, "rm", "-r", "panda")
    self.git(self.source, "update-index", "--add", "--cacheinfo", f"160000,{self.initial_commit},panda")
    self.git(self.source, "commit", "-m", "Invalid gitlink dependency")
    self.assert_rejected_before_checkout()

  def test_gitmodules_rejected_before_checkout(self):
    (self.source / ".gitmodules").write_text('[submodule "panda"]\npath = panda\nurl = https://example.invalid/panda\n')
    self.commit_source("Invalid submodule configuration")
    self.assert_rejected_before_checkout()

  def test_finalize_invalid_tree_clears_existing_readiness(self):
    self.finalized.mkdir()
    (self.finalized / ".overlay_consistent").touch()
    self.manifest["dependencies"] = []
    self.write_manifest()
    self.commit_source("Invalid empty dependency manifest")
    shutil.rmtree(self.merged)
    self.git(self.root, "clone", "--no-local", "--no-hardlinks", str(self.source), str(self.merged))
    with self.assertRaises(ValueError):
      self.updated.finalize_update()
    self.assertFalse((self.finalized / ".overlay_consistent").exists())


if __name__ == "__main__":
  unittest.main()
