from __future__ import annotations

import pytest

from bacpack.point_config import validate_units


# --- Units validation (found live in SPA testing) --------------------------
# A bad units string (e.g. "F" instead of "degreesFahrenheit") saved without
# validation used to crash build_application() with a bare bacpypes3
# ValueError, which engine.start() let propagate as an unhandled 500 --
# see validate_units's docstring and EngineManager.start()'s try/except.


def test_validate_units_accepts_real_engineering_units_names():
    validate_units("analog-input", "degreesFahrenheit")
    validate_units("analog-output", "percent")
    validate_units("analog-value", "noUnits")


def test_validate_units_accepts_missing_units():
    validate_units("analog-input", None)
    validate_units("analog-input", "")


def test_validate_units_ignores_non_analog_types():
    # Not a real unit name, but units are meaningless for binary/multi-state
    # objects -- build_bacnet_object() never even looks at it for these.
    validate_units("binary-value", "not-a-unit")
    validate_units("multi-state-value", "not-a-unit")


def test_validate_units_rejects_bad_name():
    with pytest.raises(ValueError, match="not a valid BACnet engineering unit"):
        validate_units("analog-output", "F")


# All other tests that were originally in this file have moved (see git
# blame for where). Kept as more than an empty shell (the validate_units tests
# remain) in case bacnet.py's shared mixins (WritePropertyAllowlisted,
# BacnetWriteLocked) ever need dedicated unit tests beyond what
# test_write_paths.py's cross-type tests already cover.
