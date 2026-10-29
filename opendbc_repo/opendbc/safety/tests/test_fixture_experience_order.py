"""Real predecessor/victim cases must not inherit another mode's startup input."""
import hashlib
from pathlib import Path
import unittest

from opendbc.safety.tests.libsafety import libsafety_py
from opendbc.safety.tests import test_gm_volt_cc as predecessor
from opendbc.safety.tests import test_gm_volt_cc_gas as gm
from opendbc.safety.tests import test_honda as honda


class TestFixtureExperienceOrder(unittest.TestCase):
  def original_case_after_volt_f1(self, case):
    # Keep the actual predecessor and victim assertions, native artifact and
    # binding intact. There is deliberately no reset between the two cases.
    _ = libsafety_py.libsafety
    library = libsafety_py.libpath
    digest = hashlib.sha256(Path(library).read_bytes()).hexdigest()
    predecessor.test_volt_f1_uses_six_count_threshold_and_independent_c9_without_malibu_bit()
    try:
      case.setUp()
      getattr(case, case._testMethodName)()
      case.tearDown()
    finally:
      case.doCleanups()
    self.assertEqual(libsafety_py.libpath, library)
    self.assertEqual(hashlib.sha256(Path(library).read_bytes()).hexdigest(), digest)

  def test_volt_f1_then_original_gm_pcm_rearm(self):
    self.original_case_after_volt_f1(gm.TestGmVoltCcGas('test_brake_release_requires_existing_pcm_rearm'))

  def test_volt_f1_then_original_honda_acc_hud(self):
    self.original_case_after_volt_f1(honda.TestHondaNidecPcmSafety('test_acc_hud_safety_check'))
