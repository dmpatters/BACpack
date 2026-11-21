"""Fixtures of the scale tests. They run only under --scale (the files in
this directory are skipped otherwise), so a normal run builds nothing."""
from __future__ import annotations

import shutil

import pytest

from tests.scale import datasets


@pytest.fixture(scope="session")
def install_path(request):
    return datasets.cached(datasets.FIELD, rebuild=request.config.getoption("--scale-rebuild"))


@pytest.fixture(scope="session")
def full_history_path(request):
    return datasets.cached(datasets.FULL_HISTORY, rebuild=request.config.getoption("--scale-rebuild"))


@pytest.fixture(scope="session")
def ids():
    return datasets.ids_for(datasets.FIELD)


@pytest.fixture()
def install_copy(install_path, tmp_path):
    copy = datasets.working_copy(install_path, tmp_path / "install")
    yield copy
    shutil.rmtree(copy.parent, ignore_errors=True)


@pytest.fixture()
def full_history_copy(full_history_path, tmp_path):
    copy = datasets.working_copy(full_history_path, tmp_path / "history")
    yield copy
    shutil.rmtree(copy.parent, ignore_errors=True)


@pytest.fixture(scope="module")
def install_ro(install_path, tmp_path_factory):
    """One copy of the install for a module whose scenarios only read it."""
    copy = datasets.working_copy(install_path, tmp_path_factory.mktemp("install-shared"))
    yield copy
    shutil.rmtree(copy.parent, ignore_errors=True)


@pytest.fixture()
def ctx_ro(install_ro, ids, tmp_path):
    from tests.scale.scenarios import make_context

    ctx = make_context(install_ro, tmp_path, ids)
    yield ctx
    ctx.close()


@pytest.fixture()
def ctx_rw(install_copy, ids, tmp_path):
    from tests.scale.scenarios import make_context

    ctx = make_context(install_copy, tmp_path, ids)
    yield ctx
    ctx.close()


@pytest.fixture()
def ctx_hist(full_history_copy, tmp_path):
    from tests.scale.scenarios import make_context

    ctx = make_context(full_history_copy, tmp_path, datasets.ids_for(datasets.FULL_HISTORY))
    yield ctx
    ctx.close()
