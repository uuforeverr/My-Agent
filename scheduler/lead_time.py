import math
from typing import Iterable


DEFAULT_MIN_LEAD_SECONDS = 30.0
DEFAULT_MAX_LEAD_SECONDS = 3600.0
SAFETY_SECONDS = 15.0


def calculate_lead_time(
    duration_ms: Iterable[float],
    *,
    min_seconds: float = DEFAULT_MIN_LEAD_SECONDS,
    max_seconds: float = DEFAULT_MAX_LEAD_SECONDS,
) -> float:
    """Calculate a conservative preparation window from successful history."""
    samples = sorted(max(float(value), 0.0) / 1000.0 for value in duration_ms)
    if not samples:
        return max(float(min_seconds), 300.0)
    if len(samples) < 5:
        estimate = max(samples) * 1.5 + SAFETY_SECONDS
    else:
        index = max(0, math.ceil(len(samples) * 0.95) - 1)
        estimate = samples[index] * 1.25 + SAFETY_SECONDS
    return round(min(max(estimate, float(min_seconds)), float(max_seconds)), 3)
