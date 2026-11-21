"""auth.py: scrypt hashing and the password and username rules."""
from __future__ import annotations

import pytest

from bacpack import auth


def test_hash_then_verify_round_trips():
    stored = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", stored)


def test_verify_rejects_wrong_password():
    stored = auth.hash_password("correct horse battery staple")
    assert not auth.verify_password("wrong password", stored)


def test_verify_rejects_empty_password_against_real_hash():
    stored = auth.hash_password("correct horse battery staple")
    assert not auth.verify_password("", stored)


def test_hash_and_verify_empty_password():
    # The hashing primitive doesn't judge length; check_password_rules does.
    stored = auth.hash_password("")
    assert auth.verify_password("", stored)
    assert not auth.verify_password("anything", stored)


def test_hash_and_verify_unicode_password():
    password = "paßwörd\U0001f512"
    stored = auth.hash_password(password)
    assert auth.verify_password(password, stored)


def test_two_hashes_of_same_password_differ():
    assert auth.hash_password("same password") != auth.hash_password("same password")


def test_verify_rejects_malformed_stored_hash_without_crashing():
    assert not auth.verify_password("anything", "not-a-valid-stored-hash")
    assert not auth.verify_password("anything", "")
    assert not auth.verify_password("anything", "$")
    assert not auth.verify_password("anything", "not-hex$also-not-hex")


@pytest.mark.parametrize(
    "length, ok", [(11, False), (12, True), (256, True), (257, False)]
)
def test_password_length_bounds(length, ok):
    problem = auth.check_password_rules("x" * length, "admin")
    assert (problem is None) is ok


def test_no_composition_rules():
    assert auth.check_password_rules(" " * 12, "admin") is None
    assert auth.check_password_rules("aaaaaaaaaaaa", "admin") is None


def test_password_equal_to_username_is_refused():
    assert "username" in auth.check_password_rules("administrator1", "administrator1")


def test_unicode_password_counts_characters_not_bytes():
    assert auth.check_password_rules("ß" * 12, "admin") is None
    assert auth.check_password_rules("ß" * 11, "admin") is not None


@pytest.mark.parametrize(
    "username, ok",
    [("", False), ("   ", False), ("a" * 65, False), ("ad\x00min", False), ("a" * 64, True), ("admin", True)],
)
def test_username_rules(username, ok):
    assert (auth.check_username_rules(username) is None) is ok


def test_dummy_verify_runs_scrypt_and_returns_none(monkeypatch):
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, h: calls.append(1) or real(p, h))
    assert auth.dummy_verify("whatever") is None
    assert calls == [1]


def test_is_storable_and_the_rules_refuse_an_unpaired_surrogate():
    from bacpack import auth

    assert auth.is_storable("smile \U0001f600")
    assert not auth.is_storable("a\ud800")
    assert auth.check_username_rules("a\ud800")
    assert auth.check_password_rules("a\ud800" + "x" * 20, None)
