"""Unit tests for control-group judgement math."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.control import MIN_CONTROLS, control_stats, evaluate_cycle  # noqa: E402


class ControlStatsTests(unittest.TestCase):
    def test_median_and_unscaled_mad(self):
        centre, mad, threshold = control_stats([1.0, 2.0, 3.0, 4.0], 0.1)
        self.assertAlmostEqual(centre, 2.5)
        self.assertAlmostEqual(mad, 1.0)
        self.assertAlmostEqual(threshold, 3.0)

    def test_threshold_uses_absolute_tolerance_when_mad_small(self):
        centre, mad, threshold = control_stats([2.0, 2.0, 2.0], 0.05)
        self.assertAlmostEqual(centre, 2.0)
        self.assertAlmostEqual(mad, 0.0)
        self.assertAlmostEqual(threshold, 0.05)


class EvaluateCycleTests(unittest.TestCase):
    def test_strictly_greater_than_threshold_is_outlier(self):
        # median 2, all deviations 0 -> MAD 0, threshold = tolerance 0.25
        controls = [2.0, 2.0, 2.0]
        just_at = evaluate_cycle(1, 2.25, controls, 0.25)
        self.assertEqual(just_at["status"], "normal")
        self.assertAlmostEqual(just_at["deviation_ah"], 0.25)
        beyond = evaluate_cycle(1, 2.2500001, controls, 0.25)
        self.assertEqual(beyond["status"], "outlier")
        self.assertTrue(beyond["is_outlier"])

    def test_fewer_than_three_controls_is_indeterminate(self):
        result = evaluate_cycle(1, 5.0, [2.0, 2.01], 0.001)
        self.assertEqual(result["status"], "indeterminate")
        self.assertIsNone(result["is_outlier"])
        self.assertEqual(result["control_count"], 2)

    def test_exactly_three_controls_is_judgeable(self):
        self.assertEqual(MIN_CONTROLS, 3)
        result = evaluate_cycle(1, 2.0, [2.0, 2.0, 2.0], 0.001)
        self.assertEqual(result["status"], "normal")

    def test_missing_target_cycle(self):
        result = evaluate_cycle(7, None, [2.0, 2.0, 2.0], 0.001)
        self.assertEqual(result["status"], "missing")
        self.assertIsNone(result["is_outlier"])

    def test_mad_threshold_wins_over_small_tolerance(self):
        # controls 1,2,3,4: median 2.5, MAD 1.0 -> threshold 3.0
        result = evaluate_cycle(1, 5.0, [1.0, 2.0, 3.0, 4.0], 0.01)
        self.assertEqual(result["status"], "normal")  # deviation 2.5 <= 3
        result = evaluate_cycle(1, 5.6, [1.0, 2.0, 3.0, 4.0], 0.01)
        self.assertEqual(result["status"], "outlier")  # deviation 3.1 > 3


if __name__ == "__main__":
    unittest.main(verbosity=2)
