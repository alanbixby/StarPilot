"""Large panel text uses real font metrics and matches its clipped touch bounds."""

from unittest.mock import Mock, patch

import pyray as rl
import pytest

from openpilot.starpilot.ui import clip
from openpilot.starpilot.ui.device import DeviceView
from openpilot.starpilot.ui.device_state import CONTENT_RECT as DEVICE_CONTENT_RECT, DeviceInput, DeviceRequest, DeviceState
from openpilot.starpilot.ui.presentation import BitmapFonts, FontRole, Profile, default_font_directory
from openpilot.starpilot.ui.software import SoftwareView
from openpilot.starpilot.ui.software_state import CONTENT_RECT as SOFTWARE_CONTENT_RECT, DownloadLabel, SoftwareInput, SoftwareRequest, SoftwareState


@pytest.fixture(scope="module")
def fonts():
  owns_context = not rl.is_window_ready()
  if owns_context:
    rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
    rl.init_window(128, 128, "Device and Software font fixture")
  try:
    with BitmapFonts(Profile.LARGE, default_font_directory()) as loaded:
      yield loaded
  finally:
    if owns_context:
      rl.close_window()


@pytest.fixture
def drawing(monkeypatch, fonts):
  for name in ("draw_rectangle_rounded", "draw_circle", "draw_line", "begin_scissor_mode", "end_scissor_mode"):
    monkeypatch.setattr(rl, name, Mock())
  draw = Mock()
  monkeypatch.setattr(fonts, "draw", draw)
  return draw


@pytest.mark.parametrize("version,branch,elided", (
  ("0.11.3 / fix/ui-layout-foundation / efe875f / Oct 27", "fix/" + "ui-layout-foundation/" * 5, True),
  ("0.11.3", "release", False),
))
def test_software_values_reserve_the_label_and_gap_and_stay_right_aligned(fonts, drawing, version, branch, elided):
  SoftwareView(fonts).render(SoftwareState(current_version=version, target_branch=branch))
  calls = [call.args for call in drawing.call_args_list]
  for title, original, right in (("Current Version", version, 2110), ("Target Branch", branch, 1840)):
    label = next(call for call in calls if call[0] == title)
    value = next(call for call in calls if call[0] != title and call[1] == FontRole.NORMAL and abs(call[4] - label[4]) < 1)
    text, role, size, x, _y, *_color = value
    label_right = label[3] + fonts.measure(*label[:3]).width
    assert x >= label_right + 48
    assert x + fonts.measure(text, role, size).width == pytest.approx(right)
    assert x + fonts.horizontal_ink(text, role, size)[0] >= label[3] + fonts.horizontal_ink(*label[:3])[1] + 48
    if elided:
      assert text.endswith("...") and text != original
      assert original.startswith(text[:-3])
    else:
      assert text == original


@pytest.mark.parametrize("download_label", tuple(DownloadLabel))
def test_software_capsules_share_size_unless_the_label_exceeds_the_face(fonts, drawing, download_label):
  SoftwareView(fonts).render(SoftwareState(download_label=download_label))
  labels = [call.args for call in drawing.call_args_list if call.args[1] == FontRole.MEDIUM]
  assert {call[0] for call in labels} == {"PREVIOUS", "FAST", download_label.value, "SELECT", "UNINSTALL", "VIEW"}
  for text, role, size, x, _y, *_color in labels:
    measured = fonts.measure(text, role, size).width
    assert measured <= 250
    assert size == 35 if fonts.measure(text, role, 35).width <= 250 else size < 35
    assert x >= 1260


def test_software_capsule_shrinks_only_when_a_measured_label_does_not_fit(fonts, drawing):
  text = "A LONG BUTTON LABEL"
  assert fonts.measure(text, FontRole.MEDIUM, 35).width > 250
  SoftwareView(fonts)._button(text, (1860, 427, 250, 100))
  drawn = drawing.call_args.args
  assert drawn[2] < 35
  assert fonts.measure(*drawn[:3]).width <= 250
  assert fonts.measure(text, FontRole.MEDIUM, drawn[2] + 1).width > 250


@pytest.mark.parametrize("view_type,state,handler_type,action_request,bounds", (
  (DeviceView, DeviceState(), DeviceInput, DeviceRequest.RESET_CALIBRATION, DEVICE_CONTENT_RECT),
  (SoftwareView, SoftwareState(), SoftwareInput, SoftwareRequest.OPEN_ERROR_LOG, SOFTWARE_CONTENT_RECT),
))
def test_panel_clip_and_input_include_the_last_face_and_stop_at_1040(fonts, drawing, view_type, state, handler_type, action_request, bounds):
  view_type(fonts).render(state)
  rl.begin_scissor_mode.assert_called_once_with(*bounds)
  rl.end_scissor_mode.assert_called_once_with()
  x, y, width, height = bounds
  assert y + height == 1040
  assert handler_type._target(1980, 1035, state) == action_request
  assert handler_type._target(1980, 1040, state) is None
  assert handler_type._target(x + width, 1035, state) is None
  faces = [call.args[0] for call in rl.draw_rectangle_rounded.call_args_list]
  last = next(face for face in faces if face.x == 1860 and face.y == 940)
  assert last.y + last.height <= y + height < 1070


@pytest.mark.parametrize("view_type,state", ((DeviceView, DeviceState()), (SoftwareView, SoftwareState())))
def test_panel_scissor_still_intersects_a_parent_viewport(fonts, drawing, view_type, state):
  parent = rl.Rectangle(0, 0, 2160, 1010)
  with patch.object(rl, "rl_push_matrix"), patch.object(rl, "rl_translatef"), patch.object(rl, "rl_pop_matrix"):
    with clip.placed_at(rl.Rectangle(0, -20, 2160, 1080), parent):
      view_type(fonts).render(state)
  # Moving the content within a parent must not paint beyond either viewport.
  assert rl.begin_scissor_mode.call_args_list[0].args == (550, 30, 1560, 980)
  assert rl.begin_scissor_mode.call_args_list[-1].args == (0, 0, 2160, 1010)
