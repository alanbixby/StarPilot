"""Exercise the launcher's actual build/manager block without device startup."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


def run_manager_block(tmp_path, *, build_result, prebuilt=False):
  source = (ROOT / 'launch_chffrplus.sh').read_text()
  start, end = '  # start manager\n', '  # if broken, keep on screen error\n'
  assert source.count(start) == source.count(end) == 1
  block = source.split(start, 1)[1].split(end, 1)[0]
  root = tmp_path / 'checkout with spaces'
  manager = root / 'openpilot/system/manager'
  manager.mkdir(parents=True)
  trace = tmp_path / 'calls'
  build_exit = 'kill -TERM "$$"' if build_result == 'signal' else f'exit {build_result}'
  for filename, body in (('build.py', f'echo build >> "$TEST_TRACE"\n{build_exit}\n'),
                         ('manager.py', 'echo manager >> "$TEST_TRACE"\n')):
    path = manager / filename
    path.write_text('#!/usr/bin/env bash\n' + body)
    path.chmod(0o755)
  if prebuilt:
    (root / 'prebuilt').touch()
  # Deliberately no errexit: the launcher must propagate build failure itself.
  result = subprocess.run(['bash', '-c', 'function launch {\n' + block + '\n}\nlaunch\n'],
                          cwd=root, env={**os.environ, 'DIR': str(root), 'TEST_TRACE': str(trace)},
                          capture_output=True, text=True, timeout=10)
  return result, trace.read_text().splitlines() if trace.exists() else []


@pytest.mark.parametrize('build_result', [1, 2, 130, 143, 'signal'])
def test_failed_or_interrupted_source_build_never_starts_manager(tmp_path, build_result):
  result, calls = run_manager_block(tmp_path, build_result=build_result)
  assert calls == ['build']
  assert result.returncode == (143 if build_result == 'signal' else build_result)


def test_successful_source_build_starts_manager(tmp_path):
  result, calls = run_manager_block(tmp_path, build_result=0)
  assert result.returncode == 0
  assert calls == ['build', 'manager']


def test_existing_prebuilt_path_skips_source_build(tmp_path):
  result, calls = run_manager_block(tmp_path, build_result=1, prebuilt=True)
  assert result.returncode == 0
  assert calls == ['manager']
