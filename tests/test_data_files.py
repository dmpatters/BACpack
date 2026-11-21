import os

from bacpack import data_files


def test_owner_for_is_none_when_not_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    assert data_files.owner_for(tmp_path) is None


def test_owner_for_returns_the_directory_stat_for_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    assert data_files.owner_for(tmp_path).st_uid == tmp_path.stat().st_uid


def test_hand_to_owner_calls_chown_with_uid_and_gid(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "chown", lambda *a: calls.append(a), raising=False)
    owner = tmp_path.stat()
    data_files.hand_to_owner(tmp_path, owner)
    assert calls == [(tmp_path, owner.st_uid, owner.st_gid)]


def test_hand_to_owner_is_a_no_op_without_an_owner(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "chown", lambda *a: calls.append(a), raising=False)
    data_files.hand_to_owner(tmp_path, None)
    assert calls == []
