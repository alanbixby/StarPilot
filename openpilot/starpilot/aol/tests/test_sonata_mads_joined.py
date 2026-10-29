"""Real Sonata MADS parser/Card/selfdrived/controlsd/native regression."""
import importlib
import os
from types import SimpleNamespace
from unittest.mock import patch

from opendbc.can import CANPacker
from opendbc.car import structs
from opendbc.car.hyundai.interface import CarInterface
from opendbc.car.hyundai.values import CAR
from opendbc.car.hyundai.classic_long_aol import qualified as qualified_classic
from opendbc.car.hyundai.tests.test_sonata_alpha_implicit import classic_frames
from opendbc.safety.tests.libsafety import libsafety_py
from opendbc.safety.tests.test_hyundai import checksum
from openpilot.cereal import messaging
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.selfdrive.car import card as card_module
from openpilot.starpilot.aol.tests.classic_startup_test_helpers import prepared_classic_card
from openpilot.starpilot.aol.tests.hyundai_cpp_order import hkg_pandad_order as hkg_pandad_order
from openpilot.starpilot.aol.tests.test_classic_long_profiles import params as classic_params
from openpilot.starpilot.aol.tests import test_ioniq_availability as caller_fixture
from openpilot.starpilot.aol.intent import AOL_TOGGLE
from openpilot.starpilot.aol.runtime import current_native
from openpilot.starpilot.aol.wire import PandaSlot, SafetyState, encode_safety
from openpilot.starpilot.car.hyundai.classic_long_intent import ClassicLongCardIntent
from openpilot.starpilot.car.hyundai.sonata_mads import prepare

disable_ecu_module = importlib.import_module('opendbc.car.disable_ecu')


def profile_frames(ci, tick, *, brake=False, gas=False, acc_fault=False, steer_fault=False, **inputs):
  frames = classic_frames(ci, tick, **inputs)
  packer = CANPacker('hyundai_can_generated')
  updates = {'TCS13': {'AliveCounterTCS': tick % 8, 'ACCEnable': int(acc_fault),
                       'ACC_REQ': int(inputs['engaged']), 'DriverOverride': 2 if brake else 0},
             'E_EMS11': {'CR_Vcu_AccPedDep_Pos': 1 if gas else 0},
             'MDPS12': {'CR_Mdps_StrColTq': 0, 'CR_Mdps_OutTq': 0, 'CF_Mdps_ToiFlt': int(steer_fault)}}
  replacements = {}
  for name, values in updates.items():
    frame = packer.make_can_msg(name, 0, values)
    if name == 'TCS13':
      address, data, bus = checksum(frame)
      frame = address, bytes(data), bus
    replacements[frame[0]] = frame
  return [replacements.get(frame[0], frame) for frame in frames]


def test_mads_buttons_join_parser_card_standard_state_controls_and_native(monkeypatch, hkg_pandad_order):
  identity = CAR.HYUNDAI_SONATA_HYBRID
  shared_long = True
  native = libsafety_py.libsafety
  case = caller_fixture.IoniqAvailabilityTests(methodName='runTest')
  card = None
  protocol = None
  with OpenpilotPrefix(), patch.dict(os.environ, {'SIMULATION': '0', 'AOL_REPLAY_RUNTIME': '1'}):
    saved = Params()
    saved.put_bool('OpenpilotEnabledToggle', True, block=True)
    saved.put_bool('AlwaysOnLateral', True, block=True)
    saved.put('LKASButtonControl', 8, block=True)
    saved.put('MainCruiseButtonControl', 9, block=True)
    saved.put_bool('SonataMadsButtons', True, block=True)
    saved.put('AolBrakePauseSpeedMps', 10., block=True)
    now = [10_000_000_000]
    for key in ('CancelButtonControl', 'LongCancelButtonControl', 'VeryLongCancelButtonControl'):
      saved.put(key, 8, block=True)
    try:
      saved.put_bool('AlphaLongitudinalEnabled', True, block=True)
      saved.put_bool('IsReleaseBranch', False, block=True)
      cp = prepare(classic_params(identity, lda=True), saved)
      assert qualified_classic(cp) and not cp.pcmCruise and cp.openpilotLongitudinalControl
      assert cp.lateralTuning.which() == 'torque' and CarInterface.startup_required(cp)
      card = prepared_classic_card(cp)
      assert qualified_classic(card.CP, marked_only=True)
      assert card.CP.safetyConfigs[0].safetyParam == (0xC04 if identity == CAR.HYUNDAI_SONATA else 0xC06) and card.CP.alternativeExperience == 32
      assert isinstance(card.aol_card_intent, ClassicLongCardIntent)
      assert not card.ci_initialized and not saved.get_bool('ControlsReady')
      card.slc_producer_session = 'authority-card'
      card.CI.update([])
      sd, controls, drive = case._authority_callers(False, cp=card.CP.as_reader().as_builder(), genuine_events=True)

      sd.aol_settings = card.aol_settings
      assert sd.aol_settings.lkas_action == AOL_TOGGLE and sd.aol_settings.main_action == 0
      assert sd.CP.to_dict() == card.CP.to_dict() == controls.CP.to_dict()
      assert len(card.CP.safetyConfigs) == 1 and bool(card.CP.openpilotLongitudinalControl) == shared_long
      config = card.CP.safetyConfigs[0]
      model, word = int(config.safetyModel.raw), int(config.safetyParam)
      native.set_alternative_experience(card.CP.alternativeExperience)
      assert native.set_safety_hooks(model, word) == 0
      protocol = hkg_pandad_order(native, libsafety_py.libpath)
      native.init_tests()
      native.set_alternative_experience(card.CP.alternativeExperience)
      queued, received, previous_sd = [], {}, {}
      tick = 0
      native_counts = {'nonzero': 0, 'lateral_only_nonzero': 0}
      patch_scope = monkeypatch.context()
      local = patch_scope.__enter__()
      try:
        diagnostic_requests = []

        def diagnostic_query(sender, receiver, bus, addresses, requests, responses):
          assert bus == 0 and addresses == [(0x7d0, None)]
          assert len(requests) == 1
          request = requests[0]
          assert request in (b'\x10\x03', b'\x28\x83\x01')
          diagnostic_requests.append(request)

          def receive(timeout):
            sender([(0x7d0, bytes([len(request)]) + request + bytes(7-len(request)), bus)])
            return {(0x7d0, None): b'\x50\x03'} if request == b'\x10\x03' else {}
          return SimpleNamespace(get_data=receive)
        local.setattr(disable_ecu_module, 'IsoTpParallelQuery', diagnostic_query)
        local.setattr(card_module, 'REPLAY', False)
        local.setattr(card_module.time, 'monotonic_ns', lambda: now[0])
        local.setattr(card_module.time, 'clock_gettime_ns', lambda clock: now[0])
        local.setattr(card_module.messaging, 'drain_sock_raw', lambda *args, **kwargs: list(queued))
        local.setattr(card.sm, 'update', lambda *args: None)
        create_message = messaging.new_message

        def clocked_message(service=None, size=None, **kwargs):

          kwargs.setdefault('logMonoTime', now[0])
          return create_message(service, size, **kwargs)
        local.setattr(messaging, 'new_message', clocked_message)
        send = card.pm.send

        def capture(service, event):
          raw = event if isinstance(event, bytes) else event.to_bytes()
          received[service] = messaging.log_from_bytes(raw).as_builder()
          return send(service, raw)
        local.setattr(card.pm, 'send', capture)

        def step(*, main=0, lkas=0, button=0, engaged=False, withdraw_request=False, brake=False, gas=False, acc_fault=False, steer_fault=False):
          nonlocal tick
          tick += 1
          now[0] = source_ns = 10_000_000_000 + tick * 10_000_000
          prior_axis = previous_sd.get('aolAxisState')
          prior_state = previous_sd.get('selfdriveState')
          request = 0 if prior_axis is None else (
            int(prior_axis.aolAxisState.desiredLateral) | int(prior_axis.aolAxisState.desiredLongitudinal) << 1)
          if withdraw_request:
            request = 0
          heartbeat = bool(request or (prior_state is not None and prior_state.selfdriveState.enabled))
          native.set_timer(source_ns // 1000)
          if 'aolAxisState' in previous_sd and 'aolIntentWire' in received:
            _, handshake = protocol.step(previous_sd, received, source_ns)
            assert handshake['plan'][0], handshake
            if withdraw_request:
              native.aol_set_host_request(0)
          else:
            native.set_aol_test_heartbeat(heartbeat)
            native.aol_set_host_request(request)
          frames = profile_frames(card.CI, tick, main=main, lkas=lkas, button=button, engaged=engaged,
                                  brake=brake, gas=gas, acc_fault=acc_fault, steer_fault=steer_fault)
          for address, data, bus in frames:
            assert native.safety_rx_hook(libsafety_py.make_CANPacket(address, bus, data)), (hex(address), bus)
          if tick % 100 == 0:
            native.safety_tick()
          actual_request = native.aol_get_request_mask()
          mask = native.aol_get_permission_mask()

          rx_invalid = bool(native.safety_test_toyota_rx_summary_invalid())
          relay_fault = bool(native.get_relay_malfunction())
          assert not relay_fault
          batch = messaging.log_from_bytes(card_module.can_list_to_can_capnp(frames)).as_builder()
          batch.logMonoTime = source_ns
          queued[:] = [batch.to_bytes()]
          device = messaging.new_message('deviceState', valid=True, logMonoTime=source_ns)
          device.deviceState.started, device.deviceState.startedMonoTime = True, 9_000_000_000
          pandas = messaging.new_message('pandaStates', 1, valid=True, logMonoTime=source_ns)
          panda = pandas.pandaStates[0]
          panda.safetyModel, panda.safetyParam = config.safetyModel, word
          panda.alternativeExperience = card.CP.alternativeExperience
          panda.controlsAllowed = native.get_controls_allowed()
          panda.safetyRxChecksInvalid = rx_invalid
          panda.ignitionLine = True
          slot = PandaSlot(0, 'authority-panda', model, word, card.CP.alternativeExperience,
                           native.get_controls_allowed(), safetyRxChecksInvalid=rx_invalid)
          wire = messaging.new_message('aolSafetyWire', 0, valid=True, logMonoTime=source_ns)
          wire.aolSafetyWire = encode_safety(SafetyState(1, True, source_ns, source_ns + 30_000_000,
            model, word, bool(mask & 1), bool(mask & 2), bool(actual_request & 1), bool(actual_request & 2),
            'authority-panda', card.aol_axis_session(), source_ns, (slot,)))
          messages = [device.as_reader(), pandas.as_reader(), wire.as_reader()]
          messages += [previous_sd[name] for name in ('selfdriveState', 'aolAxisState', 'onroadEvents') if name in previous_sd]
          if 'onroadEvents' not in previous_sd:
            messages.append(messaging.new_message('onroadEvents', 0, valid=True, logMonoTime=source_ns).as_reader())
          card.sm.update_msgs(source_ns / 1e9, messages)
          state, _ = card.state_update()
          assert not any(action for _, action in card.wheel_commands), 'Profile-owned buttons cannot fire saved non-axis actions'
          for service in ('carState', 'carOutput', 'aolIntentWire', 'starpilotCarState'):
            received.pop(service, None)
          card.state_publish(state, None)
          state_envelope = received['carState']
          output_envelope = received['carOutput']
          assert state_envelope.logMonoTime == source_ns and state_envelope.valid == state.canValid
          assert output_envelope.logMonoTime == source_ns

          axis, host, command = drive(tick, intent_message=received['aolIntentWire'],
            car_state_message=state_envelope, car_output_message=output_envelope,
            companion=False, authority_message=received.get('starpilotCarState'),
            allowed=bool(mask & 1), long_allowed=bool(mask & 2), native_request_lat=bool(actual_request & 1),
            native_request_long=bool(actual_request & 2), panda_controls_allowed=native.get_controls_allowed(),
            panda_fault=rx_invalid, relay_fault=relay_fault)
          if sd.conditional_car_state_valid:
            assert sd.aol_car_state_log_ns == state_envelope.logMonoTime
          assert controls.sm.logMonoTime['carState'] == state_envelope.logMonoTime
          assert controls.sm.valid['carState'] == state_envelope.valid
          assert controls.sm['carState'].to_dict() == state_envelope.carState.to_dict()
          assert controls.sm.logMonoTime['carOutput'] == output_envelope.logMonoTime
          assert controls.sm.valid['carOutput'] == output_envelope.valid
          assert controls.sm['carOutput'].to_dict() == output_envelope.carOutput.to_dict()
          previous_sd.update(sd.pm.messages)
          validated = current_native(sd.sm, sd.CP, now_ns=source_ns, axis_session_id=sd.aol_session_id)
          if not rx_invalid:
            assert validated is not None and validated.lateralAllowed == bool(mask & 1)
            assert validated.requestedLateral == bool(actual_request & 1)
          else:
            assert not axis.lateralActive and not axis.longitudinalActive
            assert not command.latActive and not command.longActive

          received.pop('sendcan', None)
          card.controls_update(state, card.sm['carControl'])
          steering = []
          if state.canValid and card.sm.valid['carControl']:
            assert 'sendcan' in received and received['sendcan'].valid
            steering = [frame for frame in received['sendcan'].sendcan
                        if frame.address == 0x340 and frame.src == 0]
            assert steering
            for frame in received['sendcan'].sendcan:
              accepted = native.safety_tx_hook(libsafety_py.make_CANPacket(frame.address, frame.src, frame.dat))
              if accepted and frame.address == 0x340 and (int.from_bytes(bytes(frame.dat)[2:4], 'little') & 0x7ff) != 1024:
                native_counts['nonzero'] += 1
                native_counts['lateral_only_nonzero'] += int(not native.get_controls_allowed())
              # The previous command may precede physical withdrawal by one frame.
              # Native LONG must reject it immediately, before the new host command.
              assert accepted or (frame.address == 0x421 and (gas or not mask & 2)) or (
                frame.address == 0x340 and not mask & 1), (tick, hex(frame.address))
          # Card applies the command received at the start of its loop. Controls
          # publishes the next command after the carState wakeup; deliver it for
          # the next state_update, preserving the ordinary cruise initialization edge.
          card.CS_prev = state
          now[0] += 1
          card.sm.update_msgs(now[0] / 1e9, [controls.pm.messages['carControl']])
          return state, axis, host, command, steering

        def settle(count=12, **inputs):
          for _ in range(count):
            result = step(**inputs)
          return result

        def assert_axes(result, *, main, lat, long, enabled=None):
          state, axis, host, command, steering = result
          assert state.cruiseState.available == main
          assert axis.lateralActive == command.latActive == lat, (axis.to_dict(), sd.events.names)
          assert axis.longitudinalActive == command.longActive == long, (
            axis.to_dict(), sd.events.names, native.get_controls_allowed(), card.v_cruise_helper.v_cruise_kph)
          assert host.enabled == (long if enabled is None else enabled)
          assert command.lateralDisplay.available
          assert command.lateralDisplay.valid
          assert command.lateralDisplay.enabled == card.aol_card_intent.allowed_latch
          return result

        def main_press():
          settle(main=1)
          return settle()

        def lane_press():
          settle(lkas=1)
          return settle()

        def engage(button=2):
          settle(4, button=button)
          return settle(30, engaged=True)

        assert_axes(settle(120), main=False, lat=False, long=False)
        assert native.safety_config_valid()
        # LFA can enable steering before the driver has ever pressed MAIN.
        assert_axes(lane_press(), main=False, lat=True, long=False)
        settle(4, button=4)
        assert_axes(settle(), main=False, lat=True, long=False)
        # MAIN ON enables, it must not toggle existing steering OFF.
        assert_axes(main_press(), main=True, lat=True, long=False)
        assert_axes(lane_press(), main=True, lat=False, long=False)
        # MAIN OFF disables, it must not toggle lane-button withdrawal ON.
        assert_axes(main_press(), main=False, lat=False, long=False)
        assert_axes(settle(30), main=False, lat=False, long=False)
        # Held/released SET with MAIN OFF cannot engage either axis.
        assert_axes(engage(), main=False, lat=False, long=False)
        assert_axes(main_press(), main=True, lat=True, long=False)
        assert_axes(lane_press(), main=True, lat=False, long=False)
        assert_axes(engage(), main=True, lat=True, long=True)
        assert_axes(settle(12, engaged=True, gas=True), main=True, lat=True, long=True)
        # Default CANCEL and Dom's brake retention retain only lateral.
        settle(4, button=4)
        assert_axes(settle(), main=True, lat=True, long=False)
        assert_axes(engage(1), main=True, lat=True, long=True)
        assert_axes(settle(12, brake=True), main=True, lat=True, long=False)
        assert_axes(settle(20), main=True, lat=True, long=False)
        assert_axes(engage(1), main=True, lat=True, long=True)
        # Native controls_allowed can retain an old SET proof over a TCS fault.
        # Actual host standard state must disarm, then healthy CAN alone cannot resume.
        settle(20, acc_fault=True)
        assert not sd.enabled and not card.aol_card_intent.allowed_latch
        assert_axes(settle(35), main=False, lat=False, long=False)
        assert_axes(main_press(), main=True, lat=True, long=False)
        assert_axes(engage(), main=True, lat=True, long=True)
        assert_axes(main_press(), main=False, lat=False, long=False)
        assert_axes(settle(40), main=False, lat=False, long=False)
        # A real EPS temporary gate retains intent for the white paused icon.
        assert_axes(lane_press(), main=False, lat=True, long=False)
        # Accept any periodic event publication before the isolated EPS edge.
        # An ambiguous event/source join must conservatively disarm instead.
        settle(3)
        result = settle(20, steer_fault=True)
        assert not result[3].latActive and result[3].lateralDisplay.enabled, (
          card.aol_card_intent.latch_clear_reason, sd.events.names, sd.aol_fault_reason,
          protocol.heartbeat, native.aol_get_permission_mask())
        assert card.aol_card_intent.allowed_latch
        assert_axes(settle(30), main=False, lat=True, long=False)
        assert_axes(lane_press(), main=False, lat=False, long=False)
        assert_axes(settle(40), main=False, lat=False, long=False)
        # Hold the real Controls snapshot beyond the source/axis lease. Display
        # mode stays selected, but stale input cannot show retained/paused intent.
        assert native_counts['nonzero'] > 0 and native_counts['lateral_only_nonzero'] > 0
        now[0] += 40_000_000
        stale, _ = controls.state_control()
        assert stale.lateralDisplay.available and not stale.lateralDisplay.valid and not stale.lateralDisplay.enabled
        assert not stale.latActive and not stale.longActive
        assert card._classic_startup_requests == (b'\x10\x03', b'\x28\x83\x01')
      finally:
        patch_scope.__exit__(None, None, None)
    finally:
      if protocol is not None:
        protocol.close()
      native.init_tests()
      native.set_alternative_experience(0)
      native.set_safety_hooks(structs.CarParams.SafetyModel.noOutput, 0)
      case.doCleanups()
      if card is not None:
        card.vehicle_startup.close()
