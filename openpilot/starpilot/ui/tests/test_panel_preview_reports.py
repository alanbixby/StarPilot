"""Panel preview reports remain JSON-safe without changing action availability."""

from dataclasses import asdict, replace
import json

import pytest

from openpilot.starpilot.ui.device_state import DeviceState
from openpilot.starpilot.ui.preview_device import state_report as device_state_report
from openpilot.starpilot.ui.preview_software import state_report as software_state_report
from openpilot.starpilot.ui.software_state import SoftwareState


@pytest.mark.parametrize("state,serialize", [
  (DeviceState(dongle_id="preview-device", offroad=False), device_state_report),
  (SoftwareState(current_version="preview-version", automatic_updates=None), software_state_report),
], ids=["device", "software"])
@pytest.mark.parametrize("availability", ["default", "restricted", "empty"])
def test_panel_preview_state_serializes_sorted_actions_without_mutation(state, serialize, availability):
  actions = sorted(state.available_actions)
  if availability == "restricted":
    state = replace(state, available_actions=frozenset((actions[-1], actions[0])))
  elif availability == "empty":
    state = replace(state, available_actions=frozenset())
  original_actions = state.available_actions
  original = asdict(state)

  report = {"state": serialize(state)}
  encoded = json.dumps(report, indent=2)
  decoded = json.loads(encoded)["state"]

  assert decoded.pop("available_actions") == sorted(action.value for action in original_actions)
  original.pop("available_actions")
  assert decoded == original
  assert state.available_actions is original_actions
  assert isinstance(state.available_actions, frozenset)
  assert asdict(state) == original | {"available_actions": original_actions}
  reordered = replace(state, available_actions=frozenset(sorted(original_actions, reverse=True)))
  assert json.dumps({"state": serialize(reordered)}, indent=2) == encoded
