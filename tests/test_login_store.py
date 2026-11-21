from __future__ import annotations

import datetime as dt
import hashlib

import pytest
from sqlalchemy import event

from bacpack import login_store as ls
from bacpack import timeutil
from bacpack.db import lock_both_files
from bacpack.models import ApiKey, WebSession, get_or_create_settings


@pytest.fixture()
def fresh(session):
    """A settings row whose mode is not chosen yet, like a new install."""
    lock_both_files(session)
    get_or_create_settings(session).login_mode = None
    session.commit()
    return session


def test_choose_mode_if_unset_only_wins_once(fresh):
    lock_both_files(fresh)
    assert ls.choose_mode_if_unset(fresh, "none") is True
    fresh.commit()
    lock_both_files(fresh)
    assert ls.choose_mode_if_unset(fresh, "login") is False
    fresh.commit()
    assert ls.get_login_state(fresh).mode == "none"


def test_set_login_ends_every_session_and_bumps_the_epoch(session):
    lock_both_files(session)
    token = ls.create_session(session, "1.1.1.1", "ua")
    before = ls.get_login_state(session).epoch
    ls.set_login(session, "admin", "hash")
    session.commit()
    state = ls.get_login_state(session)
    assert (state.mode, state.username, state.epoch) == ("login", "admin", before + 1)
    assert ls.resolve_session(session, token, timeutil.db_utc_now()) is None
    assert session.query(WebSession).count() == 0
    assert ls.password_hash_of(session) == "hash"


def test_set_no_login_ends_every_session(session):
    lock_both_files(session)
    ls.create_session(session, "1.1.1.1", None)
    ls.set_no_login(session)
    session.commit()
    assert session.query(WebSession).count() == 0
    assert ls.get_login_state(session).mode == "none"


def test_session_resolves_until_8_hours_idle(session):
    lock_both_files(session)
    token = ls.create_session(session, "1.1.1.1", None)
    session.commit()
    now = timeutil.db_utc_now()
    assert ls.resolve_session(session, token, now + dt.timedelta(hours=7, minutes=59)) is not None
    assert ls.resolve_session(session, token, now + dt.timedelta(hours=8, minutes=1)) is None


def test_the_next_login_sweeps_expired_sessions(session):
    lock_both_files(session)
    ls.create_session(session, "1.1.1.1", None)
    session.query(WebSession).update({"last_seen": timeutil.db_utc_now() - dt.timedelta(hours=9)})
    session.commit()
    lock_both_files(session)
    ls.create_session(session, "2.2.2.2", None)
    session.commit()
    assert [row.client_ip for row in session.query(WebSession)] == ["2.2.2.2"]


def test_only_the_hash_of_a_token_is_stored(session):
    lock_both_files(session)
    token = ls.create_session(session, "1.1.1.1", "agent")
    session.commit()
    row = session.query(WebSession).one()
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in " ".join(str(v) for v in (row.token_hash, row.client_ip, row.user_agent))


@pytest.mark.parametrize("token", ["", "abc", "é" * 10, "x" * 5000, "\ud800"])
def test_unknown_and_malformed_tokens_resolve_to_none(session, token):
    assert ls.resolve_session(session, token, timeutil.db_utc_now()) is None
    assert ls.resolve_api_key(session, "bpk_" + token) is None


def test_api_key_shape_and_prefix(session):
    lock_both_files(session)
    row, raw = ls.create_api_key(session, "script")
    session.commit()
    assert raw.startswith("bpk_") and len(raw) > 40
    assert row.prefix == raw[:12]
    assert row.key_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert ls.resolve_api_key(session, raw) == row.id
    assert ls.resolve_api_key(session, "nonsense") is None


def test_revoked_key_no_longer_resolves_and_leaves_the_list(session):
    lock_both_files(session)
    row, raw = ls.create_api_key(session, "script")
    session.commit()
    lock_both_files(session)
    assert ls.revoke_api_key(session, row.id) is not None
    session.commit()
    assert ls.resolve_api_key(session, raw) is None
    assert ls.list_api_keys(session) == []
    assert session.get(ApiKey, row.id).revoked_at is not None


def test_duplicate_active_key_name_refused(session):
    lock_both_files(session)
    ls.create_api_key(session, "script")
    with pytest.raises(ValueError):
        ls.create_api_key(session, "script")


def test_throttle_allows_one_write_a_minute():
    throttle = ls.WriteThrottle()
    now = timeutil.db_utc_now()
    assert throttle.due("a", now)
    assert not throttle.due("a", now + dt.timedelta(seconds=59))
    assert throttle.due("a", now + dt.timedelta(seconds=61))
    assert throttle.due("b", now)


def test_resolving_an_expired_session_writes_nothing(session_factory):
    # The middleware resolves on the event loop: a read must stay a read. The
    # next login's sweep deletes the row.
    with session_factory() as session:
        lock_both_files(session)
        token = ls.create_session(session, "1.1.1.1", None)
        session.commit()
    with session_factory() as session:
        committed = []
        event.listen(session, "after_commit", lambda s: committed.append(1))
        later = timeutil.db_utc_now() + dt.timedelta(hours=9)
        assert ls.resolve_session(session, token, later) is None
        assert committed == [] and not session.deleted
    with session_factory() as session:
        assert session.query(WebSession).count() == 1


def test_throttle_forgets_entries_older_than_the_interval():
    throttle = ls.WriteThrottle()
    now = timeutil.db_utc_now()
    for i in range(ls.WriteThrottle.MAX_ENTRIES + 500):
        throttle.due(i, now)
    later = now + dt.timedelta(minutes=2)
    throttle.due("fresh", later)
    assert len(throttle._last) <= 2
