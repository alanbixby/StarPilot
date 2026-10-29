import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from opendbc import get_generated_dbcs
from opendbc.can import CANPacker, CANParser
from opendbc.car.hyundai.escc import EsccCanFrame, EsccDecoder


FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'escc_legacy.json').read_text())
VECTORS = FIXTURE['vectors']


@pytest.mark.parametrize('vector', VECTORS, ids=lambda v: v['name'])
def test_independent_wire_vectors_through_generated_dbc_and_decoder(vector):
  data = bytes.fromhex(vector['data_hex'])
  expected = vector['expected']
  # Production parser generates this DBC from its source template in memory.
  assert 'BO_ 683 ESCC: 8' in get_generated_dbcs()['hyundai_can_generated']
  parser = CANParser('hyundai_can_generated', [('ESCC', 50)], 0)
  assert parser.update([(1_000_000_000, [(0x2AB, data, 0)])]) == {0x2AB}
  assert parser.can_valid
  assert parser.vl['ESCC'] == pytest.approx(expected)
  assert CANPacker('hyundai_can_generated').make_can_msg('ESCC', 0, expected) == (0x2AB, data, 0)
  packet = EsccDecoder().decode(EsccCanFrame(1_000_000_000, 0x2AB, data, 0))
  assert packet is not None
  assert asdict(packet) == pytest.approx(expected)


@pytest.mark.parametrize('changes', [
  {'address': 0x2AC}, {'address': 0x800002AB}, {'bus': 1}, {'bus': 2},
  {'is_extended': True}, {'is_fd': True}, {'data': b''}, {'data': bytes(7)}, {'data': bytes(9)}, {'data': bytes(64)},
  {'timestamp_ns': -1},
])
def test_decoder_rejects_wrong_envelope_before_can_parser(changes):
  decoder = EsccDecoder()
  frame = EsccCanFrame(1_000_000_000, 0x2AB, bytes.fromhex(VECTORS[1]['data_hex']), 0)
  assert decoder.decode(replace(frame, **changes)) is None
  # A rejected frame must not contaminate subsequent actual decoding.
  assert decoder.decode(frame).ACC_ObjDist == pytest.approx(22.2)


def test_reserved_bits_are_not_a_version_checksum_or_counter():
  decoder = EsccDecoder()
  data = bytearray.fromhex(VECTORS[1]['data_hex'])
  original = decoder.decode(EsccCanFrame(1, 0x2AB, bytes(data), 0))
  data[2] |= 0xF8  # bits 19..23 are unspecified in the legacy wire layout
  assert decoder.decode(EsccCanFrame(2, 0x2AB, bytes(data), 0)) == original


@pytest.mark.parametrize('signal,data_hex,value', [
  ('FCA_CmdAct', '0100000000000000', 1),
  ('CF_VSM_Warn_FCA11', '0600000000000000', 3),
  ('AEB_CmdAct', '0800000000000000', 1),
  ('CF_VSM_Warn_SCC12', '3000000000000000', 3),
  ('CF_VSM_DecCmdAct_SCC12', '4000000000000000', 1),
  ('CF_VSM_DecCmdAct_FCA11', '8000000000000000', 1),
  ('CR_VSM_DecCmd_SCC12', '00ff000000000000', 2.55),
  ('ObjValid', '0000010000000000', 1),
  ('ACC_ObjStatus', '0000060000000000', 3),
  ('ACC_ObjLatPos', '000000ff01000000', 31.1),
  ('ACC_ObjDist', '00000000fe0f0000', 204.7),
  ('ACC_ObjRelSpd', '0000000000f0ff00', 239.5),
  ('CR_VSM_DecCmd_FCA11', '00000000000000ff', 2.55),
])
def test_each_signal_boundary_without_adjacent_field_contamination(signal, data_hex, value):
  # Literal independent wire bytes ensure combined fixtures cannot hide overlap.
  expected = dict(VECTORS[0]['expected'], **{signal: value})
  packet = EsccDecoder().decode(EsccCanFrame(0, 0x2AB, bytes.fromhex(data_hex), 0))
  assert asdict(packet) == pytest.approx(expected)


def test_palisade_escc_is_not_the_deployed_layout():
  data = bytes.fromhex(VECTORS[1]['data_hex'])
  parser = CANParser('hyundai_palisade_2023_generated', [('ESCC', 50)], 0)
  parser.update([(1_000_000_000, [(0x2AB, data, 0)])])
  assert parser.vl['ESCC']['ACC_ObjDist'] == pytest.approx(122.4)
  assert parser.vl['ESCC']['ACC_ObjRelSpd'] == pytest.approx(25.3)
  assert 'ACC_ObjLatPos' not in parser.vl['ESCC']
