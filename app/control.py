"""Pure judgement math for control groups.

A cycle is judged by comparing one target discharge capacity (Ah) with the
discharge capacities of the control cells at the same cycle number.

Rules:
  * at least ``MIN_CONTROLS`` control values are required, otherwise the
    cycle is *indeterminate* (cannot judge);
  * the control centre is the median;
  * MAD is the (unscaled) median absolute deviation from that median;
  * the allowed deviation is ``max(3 * MAD, absolute_tolerance_ah)``;
  * a target is an outlier only when its absolute deviation is strictly
    greater than that limit.
"""

from __future__ import annotations

from statistics import median
from typing import Optional, Sequence

MIN_CONTROLS = 3
MAD_MULTIPLIER = 3.0


def control_stats(
    control_values: Sequence[float], abs_tolerance_ah: float
) -> tuple[float, float, float]:
    """Return (median, MAD, threshold) for a non-empty list of control Ah."""
    if not control_values:
        raise ValueError("control_values must not be empty")
    centre = float(median(control_values))
    mad = float(median([abs(float(v) - centre) for v in control_values]))
    threshold = max(MAD_MULTIPLIER * mad, float(abs_tolerance_ah))
    return centre, mad, threshold


def evaluate_cycle(
    cycle_no: int,
    target_ah: Optional[float],
    control_values: Sequence[float],
    abs_tolerance_ah: float,
) -> dict:
    """Classify one cycle.

    ``control_values`` must already exclude the target cell when the target
    is itself a control (the caller is responsible for that rule).
    """
    count = len(control_values)

    if target_ah is None:
        return {
            "status": "missing",
            "is_outlier": None,
            "reason": f"target has no cycle {cycle_no}",
            "control_count": count,
            "control_median_ah": None,
            "mad_ah": None,
            "threshold_ah": None,
            "deviation_ah": None,
        }

    if count < MIN_CONTROLS:
        return {
            "status": "indeterminate",
            "is_outlier": None,
            "reason": (
                f"insufficient control samples for cycle {cycle_no}: "
                f"{count} < {MIN_CONTROLS}"
            ),
            "control_count": count,
            "control_median_ah": None,
            "mad_ah": None,
            "threshold_ah": None,
            "deviation_ah": None,
        }

    centre, mad, threshold = control_stats(control_values, abs_tolerance_ah)
    deviation = abs(float(target_ah) - centre)
    is_outlier = deviation > threshold
    return {
        "status": "outlier" if is_outlier else "normal",
        "is_outlier": is_outlier,
        "reason": (
            f"deviation {deviation:.12g} exceeds threshold {threshold:.12g}"
            if is_outlier
            else None
        ),
        "control_count": count,
        "control_median_ah": centre,
        "mad_ah": mad,
        "threshold_ah": threshold,
        "deviation_ah": deviation,
    }
