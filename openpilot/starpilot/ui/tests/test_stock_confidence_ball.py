"""Stock compact ball follows the displayed control axes and model predictions."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pyray as rl
import pytest

from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.selfdrive.ui.mici.onroad import confidence_ball as native_ball
from openpilot.selfdrive.ui.ui_state import UIStatus
from openpilot.starpilot.ui.onroad_state import OnroadState, SpeedLimitObservation
from openpilot.starpilot.ui.runtime_app import render_stock_confidence


def rgba(color):
  return color.r, color.g, color.b, color.a


@pytest.fixture
def ball_context(monkeypatch):
  ball = native_ball.ConfidenceBall()
  # Observe the target confidence directly; smoothing is checked separately.
  ball._confidence_filter = FirstOrderFilter(-0.5, 0.5, 1 / 20, initialized=False)
  ball.set_rect(rl.Rectangle(476, 0, 60, 240))
  predictions = NS(brakeDisengageProbs=[0.1, 0.8], steerOverrideProbs=[0.2, 0.4])
  state = NS(status=UIStatus.ENGAGED, sm={"modelV2": NS(meta=NS(disengagePredictions=predictions))})
  draw = Mock()
  monkeypatch.setattr(native_ball, "ui_state", state)
  monkeypatch.setattr(native_ball, "draw_circle_gradient", draw)

  def render(_rect):
    ball._update_state()
    ball._render(None)

  monkeypatch.setattr(ball, "render", render)
  return ball, state, predictions, draw


@pytest.mark.parametrize("lateral,longitudinal,status,confidence,color", [
  (True, False, UIStatus.DISENGAGED, 0.6, (10, 186, 181, 255)),
  (False, True, UIStatus.ENGAGED, 0.2, (255, 105, 180, 255)),
  # Axis context also wins over a stale global engagement/override status.
  (True, False, UIStatus.OVERRIDE, 0.6, (10, 186, 181, 255)),
  (False, False, UIStatus.ENGAGED, -0.5, (50, 50, 50, 255)),
  (False, False, UIStatus.DISENGAGED, -0.5, (50, 50, 50, 255)),
])
def test_axis_specific_position_and_color(ball_context, lateral, longitudinal, status, confidence, color):
  ball, state, _, draw = ball_context
  state.status = status
  ball.render_with_lateral(ball.rect, lateral, longitudinal_active=longitudinal)
  assert ball._confidence_filter.x == pytest.approx(confidence)
  x, y, radius, top, bottom = draw.call_args.args
  assert (x, radius) == (512, 24)
  assert y == pytest.approx((1 - confidence) * 192 + 24)
  assert rgba(top) == color
  assert rgba(bottom) == (color if lateral or longitudinal else (13, 13, 13, 255))


@pytest.mark.parametrize("brake,steer,confidence,top,bottom", [
  ([0.1, 0.2], [0.1, 0.3], 0.56, (0, 255, 204, 255), (0, 255, 38, 255)),
  ([0.2, 0.4], [0.1, 0.5], 0.30, (255, 200, 0, 255), (255, 115, 0, 255)),
  ([0.1, 0.8], [0.2, 0.4], 0.12, (255, 0, 21, 255), (255, 0, 89, 255)),
])
def test_full_engagement_keeps_combined_confidence_zones(ball_context, brake, steer, confidence, top, bottom):
  ball, _, predictions, draw = ball_context
  predictions.brakeDisengageProbs, predictions.steerOverrideProbs = brake, steer
  ball.render_with_lateral(ball.rect, True, longitudinal_active=True)
  assert ball._confidence_filter.x == pytest.approx(confidence)
  assert tuple(map(rgba, draw.call_args.args[-2:])) == (top, bottom)


@pytest.mark.parametrize("lateral,longitudinal", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("override", ["driver_override_active", "longitudinal_overridden"])
def test_driver_override_wins_over_axis_colors(ball_context, lateral, longitudinal, override):
  ball, _, _, draw = ball_context
  ball.render_with_lateral(ball.rect, lateral, longitudinal_active=longitudinal, **{override: True})
  assert tuple(map(rgba, draw.call_args.args[-2:])) == ((255, 255, 255, 255), (82, 82, 82, 255))
  assert not ball._render_lateral_active
  assert ball._render_longitudinal_active is None
  assert not ball._render_override_active


@pytest.mark.parametrize("lateral,longitudinal", [(True, False), (False, True), (True, True)])
def test_empty_prediction_arrays_use_upstream_zero_confidence_fallback(ball_context, lateral, longitudinal):
  ball, _, predictions, _ = ball_context
  predictions.brakeDisengageProbs, predictions.steerOverrideProbs = [], []
  ball.render_with_lateral(ball.rect, lateral, longitudinal_active=longitudinal)
  assert ball._confidence_filter.x == 0


@pytest.mark.parametrize("status", [UIStatus.ENGAGED, UIStatus.OVERRIDE])
def test_legacy_callers_keep_status_based_combined_confidence(ball_context, status):
  ball, state, _, draw = ball_context
  state.status = status
  ball.render_with_lateral(ball.rect, False)
  assert ball._confidence_filter.x == pytest.approx(0.12)
  expected = ((255, 255, 255, 255), (82, 82, 82, 255)) if status == UIStatus.OVERRIDE else \
             ((255, 0, 21, 255), (255, 0, 89, 255))
  assert tuple(map(rgba, draw.call_args.args[-2:])) == expected


def test_disengaged_ball_smoothly_sinks_without_reading_predictions(ball_context):
  ball, state, _, _ = ball_context
  state.sm = {}
  ball._confidence_filter = FirstOrderFilter(0.9, 0.5, 1 / 20)
  for _ in range(120):
    ball.render_with_lateral(ball.rect, False, longitudinal_active=False)
  assert ball._confidence_filter.x == pytest.approx(-0.5, abs=0.0001)


@pytest.mark.parametrize("lateral,longitudinal,driver_override,long_override", [
  (True, False, False, False), (False, True, False, False), (True, True, True, False), (False, False, False, True),
])
def test_runtime_bridge_forwards_displayed_axes_and_override(lateral, longitudinal, driver_override, long_override):
  camera = Mock()
  rect = rl.Rectangle(476, 0, 60, 240)
  state = OnroadState(engaged=lateral or longitudinal, camera_available=True, speed_mps=12, cruise_kph=60,
                      speed_limit=SpeedLimitObservation(), lateral_active=lateral, longitudinal_active=longitudinal,
                      driver_override_active=driver_override, longitudinal_overridden=long_override)
  render_stock_confidence(camera, rect, state)
  camera.render_stock_confidence_layer.assert_called_once_with(
    rect, lateral_active=lateral, longitudinal_active=longitudinal,
    driver_override_active=driver_override, longitudinal_overridden=long_override)


def test_replay_bridge_forwards_displayed_axes_and_override(monkeypatch):
  from openpilot.starpilot.replay.render import Compositor
  from openpilot.starpilot.ui import onroad, onroad_dm

  monkeypatch.setattr(onroad, "OnroadView", Mock(return_value=Mock()))
  monkeypatch.setattr(onroad_dm, "DriverMonitorLayer", Mock())
  compositor = Compositor.__new__(Compositor)
  compositor.profile, compositor.fonts, compositor.camera = "compact", Mock(), Mock()
  compositor._new_view()
  rect = rl.Rectangle(476, 0, 60, 240)
  state = OnroadState(engaged=True, camera_available=True, speed_mps=12, cruise_kph=60,
                      speed_limit=SpeedLimitObservation(), lateral_active=False, longitudinal_active=True,
                      driver_override_active=True, longitudinal_overridden=True)
  compositor.view.stock_confidence_layer(rect, state)
  compositor.camera.render_stock_confidence_layer.assert_called_once_with(
    rect, lateral_active=False, longitudinal_active=True, driver_override_active=True, longitudinal_overridden=True)
