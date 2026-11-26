"""The UDP guard's reason for failing a test in a UDP file, per way of running."""

from types import SimpleNamespace

from tests.conftest import udp_grouping_problem

NODE = "tests/test_fakes.py::test_x"


def test_plain_run_is_fine():
    assert udp_grouping_problem(SimpleNamespace(), None, NODE) is None


def test_one_worker_is_fine():
    assert udp_grouping_problem(SimpleNamespace(loadgroup=False), {"workercount": 1}, NODE) is None


def test_several_workers_without_loadgroup_name_the_flag():
    problem = udp_grouping_problem(SimpleNamespace(loadgroup=False), {"workercount": 4}, NODE)
    assert problem is not None
    assert "--dist loadgroup" in problem
    assert "one worker" in problem


def test_loadgroup_with_the_group_suffix_is_fine():
    assert udp_grouping_problem(SimpleNamespace(loadgroup=True), {"workercount": 4}, NODE + "@bacnet-udp") is None


def test_loadgroup_without_the_group_suffix_names_the_versions():
    problem = udp_grouping_problem(SimpleNamespace(loadgroup=True), {"workercount": 4}, NODE)
    assert problem is not None
    assert "pytest/pytest-xdist versions" in problem
