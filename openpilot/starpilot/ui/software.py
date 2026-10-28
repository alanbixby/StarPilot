"""Captured six-row large Software viewport drawn from supplied state."""

import pyray as rl

from openpilot.starpilot.ui import clip

from openpilot.starpilot.ui.presentation import BitmapFonts, FontRole, Profile
from openpilot.starpilot.ui.settings_geometry import elide_text
from openpilot.starpilot.ui.software_state import CONTENT_RECT, DownloadLabel, SoftwareRequest, SoftwareState, DIRECT_BUTTONS, button_rect


TITLES = ("Current Version", "Automatically Install Updates", "Download", "Target Branch", "Uninstall", "Error Log")
VALUE_COLOR = rl.Color(170, 170, 170, 255)
BUTTON_COLOR = rl.Color(57, 57, 57, 255)
BUTTON_DISABLED = rl.Color(51, 51, 51, 255)
BUTTON_TEXT_COLOR = rl.Color(228, 228, 228, 255)
TOGGLE_ON = rl.Color(51, 171, 76, 255)


class SoftwareView:
  """Draw only the pane; the host owns the shared Settings rail."""

  def __init__(self, fonts: BitmapFonts):
    if fonts.profile != Profile.LARGE:
      raise ValueError("Only the large Software viewport has a protected capture")
    self.fonts = fonts

  def _value(self, text: str, row: int, right: float) -> None:
    if not text:
      return
    left = 570 + self.fonts.measure(TITLES[row], FontRole.NORMAL, 50).width + 48
    text = elide_text(self.fonts, text, FontRole.NORMAL, 50, right - left)
    measured = self.fonts.measure(text, FontRole.NORMAL, 50)
    top = 50 + row * 171
    self.fonts.draw(text, FontRole.NORMAL, 50, right - measured.width, top + (170 - measured.height) / 2, VALUE_COLOR)

  def _button(self, text: str, rect: tuple[int, int, int, int], enabled: bool = True) -> None:
    x, y, width, height = rect
    rl.draw_rectangle_rounded(rl.Rectangle(x, y, width, height), 1, 10, BUTTON_COLOR if enabled else BUTTON_DISABLED)
    size = 35
    measured = self.fonts.measure(text, FontRole.MEDIUM, size)
    while measured.width > width and size > 1:
      size -= 1
      measured = self.fonts.measure(text, FontRole.MEDIUM, size)
    self.fonts.draw(text, FontRole.MEDIUM, size, x + (width - measured.width) // 2,
                    y + (height - measured.height) // 2, BUTTON_TEXT_COLOR if enabled else VALUE_COLOR)

  @staticmethod
  def _toggle(enabled: bool | None, available: bool = True) -> None:
    x, y = 1950, 266
    track = TOGGLE_ON if enabled else BUTTON_COLOR
    rl.draw_rectangle_rounded(rl.Rectangle(x + 5, y + 10, 150, 60), 1, 10, track)
    rl.draw_circle(x + (80 if enabled is None else 120 if enabled else 40), y + 40, 40,
                   rl.WHITE if available else VALUE_COLOR)

  def render(self, state: SoftwareState) -> None:
    rl.draw_rectangle_rounded(rl.Rectangle(510, 10, 1640, 1060), 0.04, 30, rl.BLACK)
    clip.begin_scissor_mode(*CONTENT_RECT)
    try:
      for row, title in enumerate(TITLES):
        top = 50 + row * 171
        measured = self.fonts.measure(title, FontRole.NORMAL, 50)
        self.fonts.draw(title, FontRole.NORMAL, 50, 570, top + (170 - measured.height) // 2)
        if row == 0:
          self._value(state.current_version, row, 2110)
        elif row == 1:
          self._toggle(state.automatic_updates, SoftwareRequest.SET_AUTOMATIC_UPDATES in state.available_actions)
        elif row == 2:
          for request, rect in DIRECT_BUTTONS:
            text = "FAST" if request == SoftwareRequest.FAST_UPDATE else "PREVIOUS"
            self._button(text, rect, request in state.available_actions)
          request = SoftwareRequest.DOWNLOAD_UPDATE if state.download_label == DownloadLabel.DOWNLOAD else SoftwareRequest.CHECK_FOR_UPDATES
          self._button(state.download_label.value, button_rect(row), request in state.available_actions)
        elif row == 3:
          self._value(state.target_branch, row, 1840)
          self._button("SELECT", button_rect(row), SoftwareRequest.OPEN_BRANCH_CHOOSER in state.available_actions)
        elif row == 4:
          self._button("UNINSTALL", button_rect(row), SoftwareRequest.OPEN_UNINSTALL_CONFIRMATION in state.available_actions)
        else:
          self._button("VIEW", button_rect(row), SoftwareRequest.OPEN_ERROR_LOG in state.available_actions)
        if row == 2 and state.download_status:
          self.fonts.draw(state.download_status[:100], FontRole.NORMAL, 25, 570, top + 140, VALUE_COLOR)
        if row < len(TITLES) - 1:
          rl.draw_line(590, top + 170, 2070, top + 170, rl.GRAY)
    finally:
      clip.end_scissor_mode()
