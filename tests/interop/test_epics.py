"""bacnet-stack's EPICS tool walks every object and property of a device."""
from __future__ import annotations

import pytest

from . import parse
from .stack import IOP1, IOP2


@pytest.mark.parametrize("device", [IOP1, IOP2])
def test_epics_walk_is_clean_and_complete(stack, device):
    result = stack.tools.run("bacepics", device, timeout=90).expect_ok()
    errors = parse.parse_epics_errors(result.out)
    assert errors == [], result.describe()
    listed = parse.parse_epics_object_list(result.out)
    assert listed and (("device", device) in listed)
    walked = parse.parse_epics_objects(result.out)
    assert listed <= walked, f"object-list entries the walk did not describe: {sorted(listed - walked)}"
