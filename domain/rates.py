"""
Epidemiological rate calculations for the Aldeia Viva Saúde BIRO.

This module provides pure, side-effect-free functions for computing
population-normalised epidemiological rates.

All functions gracefully degrade (return None) when population data
is unavailable or invalid, so callers never need to guard against
ZeroDivisionError or unexpected exceptions for normal inputs.
"""


def incidence_per_100k(cases: int, population: int | None) -> float | None:
    """Return the incidence rate per 100,000 inhabitants.

    Args:
        cases:      Non-negative count of probable cases.
        population: Resident population for the territory.  When None, zero,
                    or negative the rate cannot be computed.

    Returns:
        Rate rounded to 2 decimal places, or None when population is invalid.

    Examples:
        >>> incidence_per_100k(50, 100_000)
        50.0
        >>> incidence_per_100k(0, 500_000)
        0.0
        >>> incidence_per_100k(10, None)
        # None
    """
    if population is None or population <= 0:
        return None
    return round(cases / population * 100_000, 2)
