"""Native compact labels must use codepoints requested by the application's font loader."""

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.starpilot.ui import feature_settings_compact as compact, sounds_compact
from openpilot.starpilot.ui.feature_settings_state import FeatureRow, FeatureSettingsState
from openpilot.system.ui.lib import application


@pytest.fixture
def native_codepoints(monkeypatch):
  # Run the real character selection, including translations, without a graphics context.
  requested = {}

  def load_font(path, size, codepoints, count):
    requested[Path(path).name] = {codepoints[index] for index in range(count)}
    return SimpleNamespace(texture=None)

  monkeypatch.setattr(application.rl, "load_font_ex", load_font)
  monkeypatch.setattr(application.rl, "gen_texture_mipmaps", lambda *_args: None)
  monkeypatch.setattr(application.rl, "set_texture_filter", lambda *_args: None)
  app = SimpleNamespace(_fonts={}, fallback_font=lambda: None)
  application.GuiApplication._load_fonts(app)
  return requested[application.FontWeight.BOLD] & requested[application.FontWeight.ROMAN]


def assert_native_text(text, codepoints):
  missing = sorted(set(map(ord, text)) - codepoints)
  assert not missing, f"{text!r} contains unloaded glyphs: {', '.join(f'U+{code:04X}' for code in missing)}"


@pytest.mark.parametrize("module", [compact, sounds_compact])
def test_compact_adapter_literals_use_loaded_glyphs(module, native_codepoints):
  for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
      assert_native_text(node.value, native_codepoints)


LANE_STRENGTH = FeatureRow(
  "LaneCenteringStrength", "Lane Centering Strength", "100", b"1.5",
  step=1, minimum=33, maximum=200, unit="%", available=True,
  reason="100% is normal; lower values reduce centering and higher values strengthen it",
)


@pytest.mark.parametrize("row", [
  LANE_STRENGTH,
  replace(LANE_STRENGTH, value="105"),
  replace(LANE_STRENGTH, available=False, reason="Not available with this vehicle configuration"),
  FeatureRow("LaneCenterOffset", "Lane offset", "0", step=1, minimum=-20, maximum=20, unit="cm", available=True,
             reason="Saved lane offset"),
  FeatureRow("LaneCenterOffset", "Lane offset", "0", unit="cm", reason="Turn off the vehicle to change"),
  FeatureRow("LaneCentering", "Enable Lane Centering", "On", choices=("Off", "On"), available=True),
  FeatureRow("LaneCentering", "Enable Lane Centering", "Invalid saved value", repair_value="Off", available=True),
  FeatureRow("", "Lane Centering", "Saved settings", page="lane", available=True),
  FeatureRow("reset_profiles", "Reset invalid profiles", "", available=True),
])
def test_feature_settings_cards_use_loaded_glyphs(row, native_codepoints, monkeypatch):
  class Card:
    def __init__(self, text, value):
      self.text, self.value = text, value

    def set_click_callback(self, callback):
      self.click = callback

    def set_enabled(self, enabled):
      self.enabled = enabled

  monkeypatch.setattr(compact, "BigButton", Card)
  monkeypatch.setattr(compact, "GreyBigButton", Card)
  items = []
  page = SimpleNamespace(_scroller=SimpleNamespace(items=items, add_widgets=items.extend))
  state = FeatureSettingsState(page="lane", title="Lane Centering", subtitle="Saved settings", rows=(row,))
  adapter = compact.FeatureSettingsCompact(SimpleNamespace(feature_snapshot=lambda _page: state))
  adapter._populate(page, state.page)
  for card in [adapter.entry_button(), *items]:
    assert_native_text(card.text, native_codepoints)
    assert_native_text(card.value, native_codepoints)
