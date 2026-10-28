import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.ci.check_prebuilt_receipt import check_receipt

ROOT = Path(__file__).resolve().parents[3]


class TestReleaseReceipt(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    self.repo = self.root / 'build'
    self.repo.mkdir()
    self.source = self.root / 'source'
    self.environment = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
    self.environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', PYTHONDONTWRITEBYTECODE='1',
                            BUILD_DIR=str(self.repo), SOURCE_DIR=str(self.source), RELEASE_BRANCH='fixture', BUILD_BRANCH='release')
    self.git('init', '-q', '-b', 'release')
    self.git('config', 'user.name', 'Receipt Test')
    self.git('config', 'user.email', 'receipt@example.invalid')
    self.git('config', 'core.hooksPath', os.devnull)
    for name in ('openpilot/common/prebuilt_manifest.py', 'tools/ci/check_prebuilt_receipt.py', 'tools/release/build_release.sh',
                 'tools/release/build_stripped.sh', 'tools/release/release_files.py', 'tools/release/check_file_sizes.sh'):
      destination = self.repo / name
      destination.parent.mkdir(parents=True, exist_ok=True)
      shutil.copy2(ROOT / name, destination)
    size_check = self.source / 'tools/release/check_file_sizes.sh'
    size_check.parent.mkdir(parents=True)
    shutil.copy2(ROOT / 'tools/release/check_file_sizes.sh', size_check)
    self.write('tools/vendor/check.py', '# vendor validation is outside this regression\n')
    self.write('tools/resources/check.py', '# resource validation is outside this regression\n')
    self.write('tools/release/stage_mapd_provider.py', '# Mapd staging is outside this regression\n')
    self.write('.github/workflows/release.yaml', 'name: protected workflow excluded from releases\n')
    self.write('openpilot/common/version.h', '#define COMMA_VERSION "0.10.0"\n')
    self.write('intermediate.o', 'build intermediate\n')
    self.write('openpilot/selfdrive/modeld/models/driving.onnx', 'unpackaged model\n')
    onroad = self.write('openpilot/selfdrive/test/test_onroad.py', '#!/usr/bin/env bash\nexit 0\n')
    onroad.chmod(0o755)
    (self.repo / 'prebuilt').touch()
    self.git('add', '.')
    subprocess.run(['python3', str(self.repo / 'openpilot/common/prebuilt_manifest.py'), str(self.repo)],
                   env=self.environment, check=True, capture_output=True, text=True)
    self.git('add', 'prebuilt.json')
    self.git('commit', '-qm', 'copied prebuilt source')
    self.assertEqual(check_receipt(self.repo), self.git('rev-parse', 'HEAD'))
    self.original_receipt = (self.repo / 'prebuilt.json').read_bytes()

  def git(self, *args):
    return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.environment,
                                   text=True, stderr=subprocess.PIPE).strip()

  def write(self, name, contents):
    path = self.repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return path

  def release(self):
    # Execute the actual post-build shell, including cleanup and commit. Only
    # push is intercepted; all filesystem and Git operations use the temp repo.
    script = (ROOT / 'tools/release/build_release.sh').read_text().split('# Cleanup\n', 1)[1]
    return self.shell(script, self.repo, self.environment)

  def stripped_release(self):
    self.target = self.root / 'stripped'
    self.target.mkdir()
    environment = self.environment | {'SOURCE_DIR': str(self.repo), 'TARGET_DIR': str(self.target), 'BRANCH': 'fixture'}
    # Keep the real release_files.py filtering/copy pipeline and provenance
    # writes; replace only Mapd/vendor/resource checks with fixture scripts.
    script = (ROOT / 'tools/release/build_stripped.sh').read_text().split('# do the files copy\n', 1)[1]
    setup = '''git -C "$TARGET_DIR" init -q -b tmp
git -C "$TARGET_DIR" config user.name 'Receipt Test'
git -C "$TARGET_DIR" config user.email receipt@example.invalid
git -C "$TARGET_DIR" config core.hooksPath /dev/null
'''
    return self.shell(setup + script, self.repo, environment)

  def shell(self, script, cwd, environment):
    harness = '''set -e
set -o pipefail
git() {
  for argument in "$@"; do
    if [ "$argument" = push ]; then
      touch "$SOURCE_DIR/pushed"
      return 0
    fi
  done
  command git "$@"
}
'''
    return subprocess.run(['bash', '-c', harness + script], cwd=cwd, env=environment, capture_output=True, text=True)

  def test_cleanup_commits_fresh_receipt_before_publish(self):
    result = self.release()
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertFalse((self.repo / 'tools/release').exists())
    self.assertFalse((self.repo / 'intermediate.o').exists())
    self.assertFalse((self.repo / 'openpilot/selfdrive/modeld/models/driving.onnx').exists())
    self.assertEqual(check_receipt(self.repo), self.git('rev-parse', 'HEAD'))
    self.assertNotEqual((self.repo / 'prebuilt.json').read_bytes(), self.original_receipt)
    self.assertTrue((self.source / 'pushed').exists())

  def test_new_build_outputs_are_in_committed_receipt(self):
    self.write('.gitignore', '*.so\n')
    self.write('fresh.so', 'new native build output\n')
    result = self.release()
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertEqual(self.git('show', 'HEAD:fresh.so'), 'new native build output')
    self.assertEqual(check_receipt(self.repo), self.git('rev-parse', 'HEAD'))

  def test_committed_receipt_mismatch_stops_before_publish(self):
    # Simulate a release test replacing the commit with an invalid receipt.
    # The final publication gate must inspect HEAD rather than trust the writer.
    self.write('openpilot/selfdrive/test/test_onroad.py', '''#!/usr/bin/env bash
set -e
printf '{}\\n' > prebuilt.json
git add -f prebuilt.json
git commit --amend --no-edit --quiet
''')
    result = self.release()
    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertIn('no matching build receipt', result.stderr)
    self.assertFalse((self.source / 'pushed').exists())

  def test_stripped_filtering_and_provenance_have_fresh_committed_receipt(self):
    result = self.stripped_release()
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertFalse((self.target / '.github/workflows/release.yaml').exists())
    self.assertEqual((self.target / 'git_src_commit').read_text(), self.git('rev-parse', 'HEAD'))
    self.assertEqual((self.target / 'git_src_commit_date').read_text(), self.git('show', '--no-patch', '--format=%ct %ci', 'HEAD'))
    self.assertEqual(check_receipt(self.target), subprocess.check_output(['git', '-C', str(self.target), 'rev-parse', 'HEAD'],
                                                                      env=self.environment, text=True).strip())
    self.assertNotEqual((self.target / 'prebuilt.json').read_bytes(), self.original_receipt)
    self.assertTrue((self.repo / 'pushed').exists())

  def test_stripped_committed_receipt_mismatch_stops_before_publish(self):
    self.write('tools/resources/check.py', '''from pathlib import Path
import subprocess
Path('prebuilt.json').write_text('{}')
subprocess.run(['git', 'add', '-f', 'prebuilt.json'], check=True)
subprocess.run(['git', 'commit', '--amend', '--no-edit', '--quiet'], check=True)
''')
    result = self.stripped_release()
    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
    self.assertIn('no matching build receipt', result.stderr)
    self.assertFalse((self.repo / 'pushed').exists())


if __name__ == '__main__':
  unittest.main()
