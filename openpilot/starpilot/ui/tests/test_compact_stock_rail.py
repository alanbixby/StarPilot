import copy
import unittest
from unittest.mock import Mock, patch

from openpilot.starpilot.ui import onroad
from openpilot.starpilot.ui.appearance_preferences import OnroadAppearance
from openpilot.starpilot.ui.onroad_customization import default_document
from openpilot.starpilot.ui.onroad_state import OnroadState, SpeedLimitObservation
from openpilot.starpilot.ui.presentation import BitmapFonts, Profile


class CompactStockRailTest(unittest.TestCase):
  """The stock confidence ball travels the full rail, so rail widgets must not share it."""

  def view(self):
    view = onroad.OnroadView.__new__(onroad.OnroadView)
    view.navigation = Mock()
    view.background_layer = None
    view.projection_viewport = None

    class Fonts(BitmapFonts):
      def __init__(self):
        self.profile = Profile.COMPACT
    view.fonts = Fonts()
    view.camera_layer = None
    view.pip_layer = None
    view.extra_overlays = None
    view.alert = Mock()
    view.compact_hud = Mock()
    view.compact_sidebar = Mock()
    view.torque_bar = Mock()
    view.stock_confidence_layer = Mock()
    view.stock_confidence_reset = Mock()
    view._fade = None
    return view

  def state(self, customization=None):
    return OnroadState(engaged=True, camera_available=False, speed_mps=12, cruise_kph=60,
                       speed_limit=SpeedLimitObservation(), customization=customization or default_document(),
                       appearance=OnroadAppearance(show_stock_confidence_ball=True),
                       stock_confidence_source_fresh=True, stock_confidence_source_stamp_ns=1_000_000_000,
                       stock_confidence_drive_frame=3)

  def render(self, view, state):
    with patch.object(onroad.clip, "begin_scissor_mode"), patch.object(onroad.clip, "end_scissor_mode"), \
         patch.object(onroad.rl, "draw_rectangle_rec"), patch.object(onroad.rl, "draw_rectangle_gradient_v"), \
         patch.object(onroad.rl, "draw_rectangle"), patch.object(onroad.rl, "draw_texture_ex"), \
         patch.object(onroad.rl, "draw_rectangle_rounded_lines_ex"), patch.object(onroad, "render_compact_half_borders"), \
         patch.object(view, "_prepare_compact_fade"), patch.object(view, "_slc_actions"):
      view.render(state)

  def test_default_following_distance_yields_rail_to_stock_ball(self):
    view = self.view()
    self.render(view, self.state())
    view.stock_confidence_layer.assert_called_once()
    view.compact_sidebar.render_personality.assert_not_called()

  def test_following_distance_moved_off_rail_still_renders(self):
    document = copy.deepcopy(default_document())
    document["layouts"]["compact"]["following_distance"].update(x=300, y=100)
    view = self.view()
    self.render(view, self.state(document))
    view.stock_confidence_layer.assert_called_once()
    view.compact_sidebar.render_personality.assert_called_once()


if __name__ == "__main__":
  unittest.main()
