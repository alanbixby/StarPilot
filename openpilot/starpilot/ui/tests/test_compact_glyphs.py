"""Native compact labels must use codepoints requested by the application's font loader."""

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opendbc.car.car_helpers import interfaces
from opendbc.car.gm.values import CAR as GM
from opendbc.car.hyundai.values import CAR as HYUNDAI
from openpilot.common.params import Params
from openpilot.starpilot.lateral.torque_settings import DOCUMENT_KEY, FieldChoice, PlatformProfile, serialize_document
from openpilot.starpilot.ui import feature_settings_compact as compact, sounds_compact
from openpilot.starpilot.ui.feature_settings_owner import FeatureSettingsOwner
from openpilot.starpilot.ui.feature_settings_state import FeatureRow, FeatureSettingsState, row_change
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


def test_compact_safety_set_matches_font_loader(native_codepoints):
  assert compact._compact_codepoints() == native_codepoints


@pytest.mark.parametrize("text, expected", [
  ("StarPilot’s ‘saved’ tune", "StarPilot's 'saved' tune"),
  ('“saved” "default"', '"saved" "default"'),
  ("Friction — Reset to Default", "Friction - Reset to Default"),
  ("Friction—Reset", "Friction - Reset"),
  ("range 1–10; 5 is normal", "range 1–10; 5 is normal"),
  ("Saving…", "Saving..."),
  ("Saved 1.25 · vehicle 1.50 · range 1–2", "Saved 1.25. vehicle 1.50. range 1–2"),
  ("Saved. · vehicle", "Saved. vehicle"),
  ("Saved · . · vehicle", "Saved. vehicle"),
  ("2.40 m/s²", "2.40 m/s2"),
  ("  two  spaces  ", "two spaces"),
  ("custom 🚀 label", "custom label"),
  ("left☃right", "leftright"),
])
def test_compact_text_translation(text, expected, native_codepoints):
  result = compact._compact_text(text)
  assert result == expected
  assert compact._compact_text(result) == result
  assert_native_text(result, native_codepoints)


@pytest.fixture
def render_cards(monkeypatch):
  class Card:
    def __init__(self, text, value):
      self.text, self.value = text, value

    def set_click_callback(self, callback):
      self.click = callback

    def set_enabled(self, enabled):
      self.enabled = enabled

  monkeypatch.setattr(compact, "BigButton", Card)
  monkeypatch.setattr(compact, "GreyBigButton", Card)

  def render(state):
    items = []
    page = SimpleNamespace(_scroller=SimpleNamespace(items=items, add_widgets=items.extend))
    adapter = compact.FeatureSettingsCompact(SimpleNamespace(feature_snapshot=lambda _page: state))
    adapter._populate(page, state.page)
    return [adapter.entry_button(), *items]

  return render


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
def test_feature_settings_cards_use_loaded_glyphs(row, native_codepoints, render_cards):
  state = FeatureSettingsState(page="lane", title="Lane Centering", subtitle="Saved settings", rows=(row,))
  for card in render_cards(state):
    assert_native_text(card.text, native_codepoints)
    assert_native_text(card.value, native_codepoints)


@pytest.mark.parametrize("row", [
  FeatureRow("", "‘child’", "Opening…", page="torque", available=True),
  FeatureRow("torque_reset", "‘reset’", "", available=True),
  FeatureRow("editable", "‘value’", "2", unit="m/s²", step=1, available=True, reason="Saved · vehicle"),
  FeatureRow("repair", "‘repair’", "Invalid…", repair_value="‘default’", available=True),
  FeatureRow("blocked", "‘value’", "2", unit="m/s²", reason="Saving…"),
])
def test_all_feature_card_paths_normalize_owner_text(row, native_codepoints, render_cards):
  state = FeatureSettingsState(title="‘title’", subtitle="Opening…", rows=(row,))
  cards = render_cards(state)
  assert cards[1].text == "'title'"
  assert cards[1].value == "opening..."
  for card in cards:
    assert_native_text(card.text, native_codepoints)
    assert_native_text(card.value, native_codepoints)


@pytest.fixture
def owner_states(tmp_path):
  params = Params(str(tmp_path))
  cp = interfaces[HYUNDAI.HYUNDAI_IONIQ_6].get_non_essential_params(HYUNDAI.HYUNDAI_IONIQ_6)
  cp.openpilotLongitudinalControl = True
  owner = FeatureSettingsOwner(params, lambda _group: True, vehicle_fingerprint=lambda: str(cp.carFingerprint),
                               vehicle_params=lambda: cp)

  def snapshot(page):
    return owner.snapshot(page, parked=True, system_long=True, lateral_context=True, metric=False)

  states = [snapshot("torque")]

  # A changed vehicle basis produces the real review separators and en-dash ranges.
  def save_old_torque_basis():
    tune = cp.lateralTuning.torque
    basis = (tune.latAccelFactor, tune.latAccelOffset, tune.friction)
    Path(params.get_param_path(DOCUMENT_KEY)).write_bytes(serialize_document({str(cp.carFingerprint):
      PlatformProfile(basis, FieldChoice("custom", basis[0]), FieldChoice())}))
    tune.latAccelFactor *= 1.05

  save_old_torque_basis()
  states.append(snapshot("torque"))

  # The Bolt's paused factor profile contains a curly apostrophe in its explanation.
  original_cp = cp
  cp = interfaces[GM.CHEVROLET_BOLT_CC_2018_2021].get_non_essential_params(GM.CHEVROLET_BOLT_CC_2018_2021)
  save_old_torque_basis()
  states.append(snapshot("torque"))
  cp = original_cp

  Path(params.get_param_path("RelaxedFollow")).write_bytes(b"nan")
  states.append(snapshot("relaxed"))
  Path(params.get_param_path("CurvatureData")).write_bytes(b'{"0.001":{"average":2.4,"count":2}}')
  states.append(snapshot("curve"))
  for category in ("acceleration", "braking"):
    page = "aggressive/" + category
    preset = next(row for row in snapshot(page).rows if row.key == "profile:aggressive:" + category)
    request = row_change(preset)
    assert request is not None
    assert owner.apply(replace(request, value="Custom"))
    states.append(snapshot(page))
  return states


def test_real_owner_cards_normalize_without_hiding_unmapped_glyphs(owner_states, native_codepoints, render_cards):
  original_text = []
  for state in owner_states:
    texts = [state.title, state.subtitle]
    for row in state.rows:
      texts.extend((row.label, row.value, row.unit, row.reason, row.repair_value))
    original_text.extend(texts)
    for text in texts:
      # Check before the drop fallback: a new owner glyph needs an explicit mapping.
      assert_native_text(text.translate(compact._COMPACT_TEXT_TRANSLATIONS), native_codepoints)
    for card in render_cards(state):
      assert_native_text(card.text, native_codepoints)
      assert_native_text(card.value, native_codepoints)
  assert set("’·—²") <= set("".join(original_text))
