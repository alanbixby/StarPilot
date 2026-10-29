"""Profile opt-in through the real settings gateway preserves wheel preferences."""
from dataclasses import replace
import tempfile

from opendbc.car.hyundai.values import CAR
from openpilot.common.params import Params
from openpilot.starpilot.aol.tests.test_classic_long_profiles import params as vehicle_params
from openpilot.starpilot.galaxy.settings import AuthorityContext, SettingsGateway


def test_mads_profile_settings_and_superseded_actions():
  with tempfile.TemporaryDirectory() as directory:
    params = Params(directory)
    params.put_bool('AlwaysOnLateral', True, block=True)
    cp = vehicle_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True)
    initial_cp = cp.to_dict()
    context = AuthorityContext(True, cp, cp.to_bytes())

    class Authority:
      def sample(self):
        return context

    gateway = SettingsGateway(params, Authority(), clock=lambda: 100.)

    def page(name='aol'):
      return gateway.page(name, 'session', b'generation')

    def toggle(value):
      view = page()
      index = next(i for i, row in enumerate(view['rows']) if row['label'] == 'Sunnypilot MADS Buttons')
      intent = gateway.preview(view['view'], index, 0, 'session', b'generation', value=value)
      return gateway.confirm(intent['intent'], 'session', b'generation')

    rows = page()['rows']
    master = next(i for i, row in enumerate(rows) if row['label'] == 'Enable Always On Lateral')
    assert rows[master + 1]['label'] == 'Sunnypilot MADS Buttons'
    assert rows[master + 1]['value'] == rows[master + 1]['defaultValue'] == 'Off'
    params.put('MainCruiseButtonControl', 9, block=True)
    params.put('LKASButtonControl', 8, block=True)
    params.put('CancelButtonControl', 8, block=True)
    params.put('AolBrakePauseSpeedMps', 10., block=True)
    keys = ('MainCruiseButtonControl', 'LKASButtonControl', 'CancelButtonControl', 'AolBrakePauseSpeedMps')
    before = {key: params.get(key) for key in keys}
    assert toggle('On')
    assert cp.to_dict() == initial_cp, 'Saving does not change this drive'
    assert next(row for row in page()['rows'] if row['label'] == 'Brake pause below')['value'] == 'Set by MADS'
    wheel = page('wheel')['rows']
    overridden = [row for row in wheel if row['label'] in ('LKAS press', 'Main cruise press', 'Cancel short press',
                                                          'Cancel long press', 'Cancel very long press')]
    assert len(overridden) == 5
    assert all(row['value'] == 'Set by MADS' and not row['available'] for row in overridden)
    assert {key: params.get(key) for key in keys} == before
    # Master-off restores effective ownership without erasing the saved profile.
    view = page()
    index = next(i for i, row in enumerate(view['rows']) if row['label'] == 'Sunnypilot MADS Buttons')
    pending = gateway.preview(view['view'], index, 0, 'session', b'generation', value='Off')
    params.put_bool('AlwaysOnLateral', False, block=True)
    assert not gateway.confirm(pending['intent'], 'session', b'generation')
    row = next(row for row in page()['rows'] if row['label'] == 'Sunnypilot MADS Buttons')
    assert row['value'] == 'On' and not row['available']
    assert next(row for row in page()['rows'] if row['label'] == 'Brake pause below')['value'] != 'Set by MADS'
    assert next(row for row in page('wheel')['rows'] if row['label'] == 'LKAS press')['value'] == 'Bookmark'
    assert params.get_bool('SonataMadsButtons')
    assert {key: params.get(key) for key in keys} == before
    params.put_bool('AlwaysOnLateral', True, block=True)
    assert next(row for row in page()['rows'] if row['label'] == 'Brake pause below')['value'] == 'Set by MADS'
    context = replace(context, parked=False)
    assert not next(row for row in page()['rows'] if row['label'] == 'Sunnypilot MADS Buttons')['available']
    context = replace(context, parked=True)
    assert toggle('Off')
    assert {key: params.get(key) for key in keys} == before
    assert next(row for row in page('wheel')['rows'] if row['label'] == 'LKAS press')['value'] == 'Bookmark'
    for invalid in (vehicle_params(CAR.HYUNDAI_SONATA, lda=True), vehicle_params(CAR.HYUNDAI_SONATA_HYBRID, lda=False),
                    vehicle_params(CAR.HYUNDAI_SONATA_HYBRID, lda=True, release=True)):
      context = AuthorityContext(True, invalid, invalid.to_bytes())
      assert not any(row['label'] == 'Sunnypilot MADS Buttons' for row in page()['rows'])
