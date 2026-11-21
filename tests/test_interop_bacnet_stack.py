from __future__ import annotations

import io
import tarfile
from types import SimpleNamespace

import pytest

from tests.interop import bacnet_stack as bs


def _make_tarball(path, members):
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


@pytest.fixture()
def good_tarball(tmp_path, monkeypatch):
    path = tmp_path / "t.tar.gz"
    _make_tarball(path, {f"{bs.TOP_FOLDER}/Makefile": b"all:\n"})
    monkeypatch.setattr(bs, "SHA256", bs.sha256_of(path).upper())
    return path


def test_hash_mismatch_names_both_hashes(tmp_path):
    path = tmp_path / "x"
    path.write_bytes(b"nope")
    with pytest.raises(bs.InteropSetupError) as err:
        bs.verify_tarball(path)
    assert bs.sha256_of(path).upper() in str(err.value) and bs.SHA256 in str(err.value)


def test_hash_check_ignores_case(good_tarball, monkeypatch):
    monkeypatch.setattr(bs, "SHA256", bs.SHA256.lower())
    bs.verify_tarball(good_tarball)


def test_missing_tarball_and_failed_download_gives_the_manual_step(tmp_path):
    def fail(url, dest):
        raise OSError("no route")

    with pytest.raises(bs.InteropSetupError, match="scp"):
        bs.ensure_tarball(tmp_path, download=fail)
    assert not list(tmp_path.glob("*.part"))


def test_download_is_verified_and_kept(tmp_path, good_tarball):
    def fetch(url, dest):
        dest.write_bytes(good_tarball.read_bytes())

    cache = tmp_path / "cache"
    assert bs.ensure_tarball(cache, download=fetch) == cache / bs.TARBALL
    assert bs.ensure_tarball(cache, download=None) == cache / bs.TARBALL  # cached: no download


def test_download_with_wrong_hash_is_an_error_and_is_not_kept(tmp_path):
    def fetch(url, dest):
        dest.write_bytes(b"junk")

    with pytest.raises(bs.InteropSetupError, match="SHA-256"):
        bs.ensure_tarball(tmp_path, download=fetch)
    assert list(tmp_path.iterdir()) == []


def test_cached_tarball_with_wrong_hash_says_to_delete_it(tmp_path):
    (tmp_path / bs.TARBALL).write_bytes(b"junk")
    with pytest.raises(bs.InteropSetupError, match="delete the file"):
        bs.ensure_tarball(tmp_path, download=None)


def test_unpack_turns_a_refused_member_into_a_setup_error_and_cleans_up(tmp_path):
    path = tmp_path / "t.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(f"{bs.TOP_FOLDER}/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"  # the "data" filter refuses a link out of the tree
        tar.addfile(info)
    cache = tmp_path / "cache"
    cache.mkdir()
    with pytest.raises(bs.InteropSetupError, match="cannot unpack"):
        bs.unpack(path, cache)
    assert list(cache.iterdir()) == []


def test_unpack_extracts_and_is_idempotent(tmp_path, good_tarball):
    tree = bs.unpack(good_tarball, tmp_path)
    assert (tree / "Makefile").is_file()
    assert bs.unpack(good_tarball, tmp_path) == tree


def test_unpack_refuses_paths_outside_the_top_folder(tmp_path):
    path = tmp_path / "evil.tar.gz"
    _make_tarball(path, {"../evil": b"x"})
    with pytest.raises(bs.InteropSetupError, match="unexpected path"):
        bs.unpack(path, tmp_path / "c")
    assert not (tmp_path / "evil").exists()


def _fill_bin(tree, names):
    (tree / "bin").mkdir(parents=True, exist_ok=True)
    for name in names:
        file = tree / "bin" / name
        file.write_bytes(b"")
        file.chmod(0o755)


def test_tools_built(tmp_path):
    assert not bs.tools_built(tmp_path)
    _fill_bin(tmp_path, bs.TOOLS[:-1])
    assert not bs.tools_built(tmp_path)
    _fill_bin(tmp_path, bs.TOOLS)
    assert bs.tools_built(tmp_path)


def test_ensure_built_runs_make_only_when_needed(tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        _fill_bin(tmp_path, bs.TOOLS)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    bs.ensure_built(tmp_path, run=run)
    bs.ensure_built(tmp_path, run=run)
    assert calls == [["make", "clean", "all"]]
    bs.ensure_built(tmp_path, rebuild=True, run=run)
    assert len(calls) == 2


def test_build_failure_shows_the_output_tail(tmp_path):
    def run(cmd, **kwargs):
        return SimpleNamespace(returncode=2, stdout="a\n" * 100 + "boom line\n", stderr="")

    with pytest.raises(bs.InteropSetupError, match="boom line"):
        bs.ensure_built(tmp_path, run=run)


def test_toolchain_check_names_everything_missing():
    with pytest.raises(bs.InteropSetupError) as err:
        bs.require_toolchain(platform="win32", which=lambda name: None)
    text = str(err.value)
    assert "Linux" in text and "gcc" in text and "make" in text and "stdbuf" in text
    bs.require_toolchain(platform="linux", which=lambda name: "/usr/bin/" + name)
