"""The ESCC present/unqualified guard through the actual Card constructor.

A classic Sonata Hybrid fingerprinted with 0x2AB on bus 0 and a saved alpha
long choice must publish a passive noOutput CarParams with no startup owner,
no ECU diagnostic request and no controller output. This is a dashcam veto, not
an ESCC owner or a qualified stock-ACC fallback; see
docs/research/escc-active-owner-spec.md.
"""
import gc
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from opendbc.car import car_helpers, gen_empty_fingerprint, structs
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.values import CAR
from opendbc.car.structs import car
from openpilot.cereal import log
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.selfdrive.car.card import Car

EventName = log.OnroadEvent.EventName


def sonata_fingerprint(*, escc):
  fp = gen_empty_fingerprint()
  fp[0][0x391] = 8
  fp[0][0x38D] = 8
  if escc:
    fp[0][0x2AB] = 8
  return fp


class _DiagnosticRecorder:
  calls: list = []

  def __init__(self, send, recv, bus, addresses, requests, responses, **kwargs):
    self.calls.append((bus, addresses, requests))

  def get_data(self, timeout):
    return {}


def construct(*, escc, alpha_long, mads=False):
  params = Params()
  for name, value in (('OpenpilotEnabledToggle', True), ('SafeMode', False), ('AlwaysOnLateral', mads), ('SonataMadsButtons', mads),
                      ('IsReleaseBranch', False), ('AlphaLongitudinalEnabled', alpha_long), ('IsOffroad', False)):
    params.put_bool(name, value, block=True)
  assert params.get_bool('SonataMadsButtons') == mads
  params.put('MainCruiseButtonControl', 0, block=True)
  params.put('LKASButtonControl', 0, block=True)
  sends, inits, factory_cps = [], [], []
  _DiagnosticRecorder.calls = []
  fingerprint = (CAR.HYUNDAI_SONATA_HYBRID, sonata_fingerprint(escc=escc), '0' * 17, [],
                 structs.CarParams.FingerprintSource.can, True)
  real_factory = CarInterface.startup_owner
  def factory(cp, callbacks, *, requested):
    owner = real_factory(cp, callbacks, requested=requested)
    factory_cps.append((cp.as_reader().to_dict(), requested, owner))
    return owner
  host = Car.__new__(Car)
  try:
    with patch.object(car_helpers, 'fingerprint', return_value=fingerprint), \
         patch('openpilot.selfdrive.car.card.messaging.recv_one_retry', return_value=SimpleNamespace(can=[1])), \
         patch('openpilot.selfdrive.car.card.can_comm_callbacks', return_value=(lambda wait_for_one=False: [], sends.extend)), \
         patch('opendbc.car.disable_ecu.IsoTpParallelQuery', _DiagnosticRecorder), \
         patch('opendbc.car.hyundai.ecu_startup.IsoTpParallelQuery', _DiagnosticRecorder), \
         patch.object(CarInterface, 'startup_owner', staticmethod(factory)), \
         patch.object(CarInterface, 'init', staticmethod(lambda *args, **kwargs: inits.append(args))):
      Car.__init__(host)
    return host, params, sends, inits, factory_cps
  except BaseException:
    if getattr(host, 'vehicle_startup', None) is not None:
      host.vehicle_startup.close()
    raise


def drive_steps(host, *, initializing):
  """Run the real Card.step branching with stubbed IO and record controller calls."""
  events = [SimpleNamespace(name=EventName.selfdriveInitializing)] if initializing else []
  host.sm = type('SM', (), {'seen': {'onroadEvents': True}, '__getitem__': lambda self, name: events})()
  host._deferred_radar_can = None
  controls, applies = [], []
  with patch.object(host, 'state_update', return_value=(car.CarState.new_message(), None)), \
       patch.object(host, 'state_publish'), patch.object(host, 'radar_update', return_value=None), \
       patch.object(host, 'radar_publish'), \
       patch.object(host, 'controls_update', side_effect=lambda *a, **k: controls.append(k)), \
       patch.object(host.CI, 'apply', side_effect=lambda *a, **k: applies.append(a)):
    for _ in range(5):
      host.step()
  return controls, applies


@pytest.fixture
def isolated():
  with tempfile.TemporaryDirectory(prefix='escc-guard-') as temporary, \
       patch.dict(os.environ, {'PARAMS_ROOT': temporary}), OpenpilotPrefix():
    try:
      yield
    finally:
      gc.collect()


@pytest.mark.parametrize('mads', (False, True))
def test_actual_card_publishes_passive_no_output_with_escc_and_alpha_long(isolated, mads):
  host, params, sends, inits, factory_cps = construct(escc=True, alpha_long=True, mads=mads)
  try:
    assert host.CP.carFingerprint == CAR.HYUNDAI_SONATA_HYBRID
    assert host.CP.dashcamOnly and host.CP.passive
    assert not host.CP.openpilotLongitudinalControl and host.CP.pcmCruise
    assert host.CP.alphaLongitudinalAvailable, 'the unsupported request stays visible'
    assert [c.safetyModel for c in host.CP.safetyConfigs] == [structs.CarParams.SafetyModel.noOutput]
    assert [int(c.safetyParam) for c in host.CP.safetyConfigs] == [0]
    assert host.CP.alternativeExperience == 0
    # A saved MADS choice neither restores a profile nor qualifies AOL here.
    assert not host.aol_qualified and host.aol_card_intent is None and not host.sonata_mads_buttons
    # The prepublication factory saw only the guarded CP, was asked with the
    # saved request, and chose no owner; no ECU request or frame was sent.
    assert len(factory_cps) == 1
    seen_cp, requested, owner = factory_cps[0]
    assert requested and owner is None and seen_cp['dashcamOnly'] and not seen_cp['openpilotLongitudinalControl']
    assert host.vehicle_startup.owner is None
    assert not CarInterface.startup_required(host.CP)
    assert _DiagnosticRecorder.calls == [] and sends == [] and inits == []
    # No Card branch reaches the controller, initialized or initializing.
    for initializing in (False, True):
      assert drive_steps(host, initializing=initializing) == ([], [])
    assert inits == [] and sends == []
    with structs.CarParams.from_bytes(params.get('CarParams')) as stored:
      assert stored.to_dict() == host.CP.as_reader().to_dict()
  finally:
    host.vehicle_startup.close()


@pytest.mark.parametrize('mads', (False, True))
def test_actual_card_stock_path_with_escc_matches_no_escc(isolated, mads):
  published = []
  for escc in (False, True):
    host, params, sends, inits, factory_cps = construct(escc=escc, alpha_long=False, mads=mads)
    try:
      assert not host.CP.dashcamOnly and not host.CP.passive
      assert not host.CP.openpilotLongitudinalControl and host.CP.pcmCruise
      assert host.vehicle_startup.owner is None
      assert _DiagnosticRecorder.calls == [] and sends == [] and inits == []
      published.append((host.CP.as_reader().to_dict(), host.aol_qualified, host.sonata_mads_buttons))
    finally:
      host.vehicle_startup.close()
  # Stock ACC and MADS are not physically qualified with the interceptor; this
  # only shows the guard leaves the existing stock profile unchanged.
  assert published[0] == published[1]
