"""Compact alerts fit their allocation using the reviewed bitmap font assets."""

from dataclasses import dataclass, replace
from unittest.mock import patch

import pyray as rl
import pytest

from openpilot.starpilot.ui.onroad_alerts import AlertRenderer
from openpilot.starpilot.ui.onroad_state import AlertSize, OnroadAlert
from openpilot.starpilot.ui.presentation import BitmapFonts, FontRole, Profile, default_font_directory


@dataclass(frozen=True)
class DrawnText:
  text: str
  role: FontRole
  size: float
  x: float
  y: float
  spacing: float


@pytest.fixture(scope='module')
def fonts():
  # Match test_dinish_speed.py: font textures need a live Raylib context.
  owns_context = not rl.is_window_ready()
  if owns_context:
    rl.init_window(128, 128, 'Compact alert font fixture')
  try:
    with BitmapFonts(Profile.COMPACT, default_font_directory()) as bitmap_fonts:
      yield bitmap_fonts
  finally:
    if owns_context:
      rl.close_window()


def render(fonts, alert):
  rect = rl.Rectangle(0, 0, 476, 240)
  with patch.object(fonts, 'draw') as draw, patch.object(rl, 'draw_rectangle'), patch.object(rl, 'draw_rectangle_gradient_v'):
    AlertRenderer(fonts).render(rect, alert)
  calls = [DrawnText(*call.args[:5], call.kwargs['spacing']) for call in draw.call_args_list]
  return rect, calls


def drawn_lines(fonts, calls):
  # Also support a draw containing explicit newlines, with Raylib's line step.
  for call in calls:
    lines = call.text.split('\n')
    measured = fonts.measure(call.text, call.role, call.size, spacing=call.spacing)
    first = fonts.measure(lines[0], call.role, call.size, spacing=call.spacing)
    step = (measured.height - first.height) / max(1, len(lines) - 1)
    for index, line in enumerate(lines):
      yield replace(call, text=line, y=call.y + index * step)


def assert_fits(fonts, rect, calls):
  assert calls
  for line in drawn_lines(fonts, calls):
    measured = fonts.measure(line.text, line.role, line.size, spacing=line.spacing)
    ink_top, ink_bottom = fonts.vertical_ink(line.text, line.role, line.size)
    assert rect.x + 18 <= line.x
    assert measured.width <= rect.width - 36
    assert line.x + measured.width <= rect.x + rect.width - 18
    assert rect.y <= line.y <= line.y + measured.height <= rect.y + rect.height
    assert rect.y <= line.y + ink_top <= line.y + ink_bottom <= rect.y + rect.height
    assert line.size >= 30


def normalized(text):
  return ' '.join(text.lower().split())


@pytest.mark.parametrize('alert', [
  # selfdrive/selfdrived/events.py:414: preserve the real trailing space and paragraphs.
  OnroadAlert(AlertSize.MID, 'Big Model Failed ', 'Restart the car to retry,\nsmall model is still available'),
  # selfdrive/selfdrived/events.py:546: driverUnresponsive1's actual small alert.
  OnroadAlert(AlertSize.SMALL, 'Touch Steering Wheel: No Face Detected'),
  # selfdrive/selfdrived/events.py:820 and :150-158: NoEntryAlert swaps text on mici.
  OnroadAlert(AlertSize.MID, 'Calibration Invalid: Remount Device & Recalibrate', 'openpilot Unavailable'),
], ids=['big-model-failed', 'no-face-detected', 'calibration-invalid'])
def test_real_compact_alerts_keep_complete_titles_and_fit(fonts, alert):
  rect, calls = render(fonts, alert)
  assert_fits(fonts, rect, calls)
  titles = list(drawn_lines(fonts, [call for call in calls if call.role == FontRole.DISPLAY]))
  assert 1 <= len(titles) <= 3
  assert all(line.size > 30 for line in titles)
  assert normalized(' '.join(line.text for line in titles)) == normalized(alert.text1)
  subtitles = list(drawn_lines(fonts, [call for call in calls if call.role == FontRole.ROMAN]))
  if alert.text2:
    assert subtitles
    assert normalized(' '.join(line.text for line in subtitles)) == normalized(alert.text2)
    last_title = titles[-1]
    title_bottom = last_title.y + fonts.measure(last_title.text, last_title.role, last_title.size, spacing=last_title.spacing).height
    assert subtitles[0].y == pytest.approx(title_bottom - 4)
  else:
    assert not subtitles
  if '\n' in alert.text2:
    first_paragraph = normalized(alert.text2.split('\n')[0])
    prefix = ''
    for index, line in enumerate(subtitles):
      prefix = normalized(f'{prefix} {line.text}')
      if len(prefix) >= len(first_paragraph):
        assert prefix == first_paragraph
        assert index + 1 < len(subtitles)
        break
    else:
      pytest.fail('The explicit first subtitle paragraph was lost')


@pytest.mark.parametrize('subtitle', ['', 'Ready'])
@pytest.mark.parametrize('text', ['Baseline', 'Baseline alert'])
def test_baseline_keeps_single_title_draw_and_original_subtitle_position(fonts, subtitle, text):
  rect, calls = render(fonts, OnroadAlert(AlertSize.MID, text, subtitle))
  assert_fits(fonts, rect, calls)
  title_calls = [call for call in calls if call.role == FontRole.DISPLAY]
  assert len(title_calls) == 1
  title = title_calls[0]
  text = text.lower()
  preferred_size = 82 if len(text) <= 12 else 70
  assert fonts.measure(text, FontRole.DISPLAY, preferred_size, spacing=-0.02 * preferred_size).width <= rect.width - 36
  assert title == DrawnText(text, FontRole.DISPLAY, preferred_size, 18, 12, -0.02 * preferred_size)
  subtitle_calls = [call for call in calls if call.role == FontRole.ROMAN]
  if subtitle:
    old_y = 12 + fonts.measure(text, FontRole.DISPLAY, preferred_size, spacing=-0.02 * preferred_size).height - 4
    assert subtitle_calls == [DrawnText('ready', FontRole.ROMAN, 36, 18, old_y, 36 * 0.025)]
  else:
    assert not subtitle_calls


@pytest.mark.parametrize('title,subtitle', [
  ('Harness Relay Malfunction', 'Contact comma.ai/support'),
  ('Vehicle Sensors Calibrating', 'Drive to Calibrate'),
])
def test_medium_real_titles_wrap_at_the_larger_start_size(fonts, title, subtitle):
  rect, calls = render(fonts, OnroadAlert(AlertSize.MID, title, subtitle))
  assert_fits(fonts, rect, calls)
  titles = [call for call in calls if call.role == FontRole.DISPLAY]
  assert len(titles) == 2
  assert {line.size for line in titles} == {54}
  assert normalized(' '.join(line.text for line in titles)) == normalized(title)


def test_title_preserves_explicit_newlines_and_reserves_minimum_subtitle_line(fonts):
  rect, calls = render(fonts, OnroadAlert(AlertSize.MID, 'Big\nRed\nCar', 'Ready'))
  assert_fits(fonts, rect, calls)
  titles = [call for call in calls if call.role == FontRole.DISPLAY]
  assert [line.text for line in titles] == ['big', 'red', 'car']
  size = titles[0].size
  assert 30 < size < 82
  subtitles = [call for call in calls if call.role == FontRole.ROMAN]
  assert len(subtitles) == 1
  assert subtitles[0].size == 30
  # The next larger size must fail the same three-line title plus one subtitle line.
  larger_height = fonts.measure('big', FontRole.DISPLAY, size + 2, spacing=-.02 * (size + 2)).height
  subtitle_height = fonts.measure('ready', FontRole.ROMAN, 30, spacing=.75).height
  assert 12 + larger_height * (1 + 2 * .86 * .9) - 4 + subtitle_height > rect.height


@pytest.mark.parametrize('title', [
  'W' * 120,
  'Calibration invalid: remount device and recalibrate before restarting the car and attempting to engage again. ' * 10,
], ids=['unbroken-word', 'more-than-three-lines'])
def test_excess_title_is_elided_within_two_lines(fonts, title):
  rect, calls = render(fonts, OnroadAlert(AlertSize.SMALL, title))
  assert_fits(fonts, rect, calls)
  titles = list(drawn_lines(fonts, [call for call in calls if call.role == FontRole.DISPLAY]))
  assert 1 <= len(titles) <= 2
  assert titles[-1].text.endswith(('…', '...'))
  visible = ''.join(line.text for line in titles).rstrip('….')
  assert ''.join(title.lower().split()).startswith(''.join(visible.lower().split()))


def test_subtitle_overflow_preserves_title_and_elides_the_tail(fonts):
  title = 'Calibration Invalid: Remount Device & Recalibrate'
  subtitle = 'Restart the car to retry.\n' + 'The small model is still available. ' * 40
  rect, calls = render(fonts, OnroadAlert(AlertSize.MID, title, subtitle))
  assert_fits(fonts, rect, calls)
  titles = list(drawn_lines(fonts, [call for call in calls if call.role == FontRole.DISPLAY]))
  assert normalized(' '.join(line.text for line in titles)) == normalized(title)
  subtitles = list(drawn_lines(fonts, [call for call in calls if call.role == FontRole.ROMAN]))
  assert subtitles
  assert {line.size for line in subtitles} == {30}
  assert subtitles[-1].text.endswith(('…', '...'))
  visible = ' '.join(line.text for line in subtitles).rstrip('….')
  assert normalized(subtitle).startswith(normalized(visible))


def test_unchanged_alert_reuses_measured_layout(fonts):
  renderer = AlertRenderer(fonts)
  alert = OnroadAlert(AlertSize.SMALL, 'Touch Steering Wheel: No Face Detected')
  rect = rl.Rectangle(0, 0, 476, 240)
  with patch.object(fonts, 'measure', wraps=fonts.measure) as measure, patch.object(fonts, 'draw'), \
       patch.object(rl, 'draw_rectangle'), patch.object(rl, 'draw_rectangle_gradient_v'):
    renderer.render(rect, alert)
    count = measure.call_count
    assert count > 0
    renderer.render(rect, alert)
    assert measure.call_count == count
