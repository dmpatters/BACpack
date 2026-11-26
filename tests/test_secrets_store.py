import json
import logging
import os
import stat

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from bacpack import secrets_store as ss
from bacpack.db import init_db, make_engine
from bacpack.models import Connector
from bacpack.secrets_store import SecretKeyProblem, Unreadable


def _replace_key(data_dir):
    (data_dir / ss.KEY_FILE).unlink()
    ss._fernets.clear()
    ss.ensure_key(data_dir)


def test_ensure_key_creates_the_file_once(tmp_path):
    assert ss.ensure_key(tmp_path) is True
    first = (tmp_path / ss.KEY_FILE).read_bytes()
    assert ss.ensure_key(tmp_path) is False
    assert (tmp_path / ss.KEY_FILE).read_bytes() == first


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_key_file_is_owner_only_on_posix(tmp_path):
    ss.ensure_key(tmp_path)
    assert stat.S_IMODE((tmp_path / ss.KEY_FILE).stat().st_mode) == 0o600


# FAT/exFAT, some SMB shares and container bind mounts refuse hard links
# with a plain OSError, not FileExistsError.
def _links_refused(monkeypatch, before_raising=None):
    def refuse(src, dst):
        if before_raising is not None:
            before_raising()
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(ss.os, "link", refuse)


def _leftover_temps(data_dir):
    return [p.name for p in data_dir.iterdir() if p.name.endswith(".tmp")]


def test_a_folder_that_refuses_hard_links_still_gets_a_usable_key(tmp_path, monkeypatch):
    _links_refused(monkeypatch)
    assert ss.ensure_key(tmp_path) is True
    assert ss.decrypt(tmp_path, ss.encrypt(tmp_path, "pw")) == "pw"
    assert _leftover_temps(tmp_path) == []
    first = (tmp_path / ss.KEY_FILE).read_bytes()
    assert ss.ensure_key(tmp_path) is False
    assert (tmp_path / ss.KEY_FILE).read_bytes() == first


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_key_made_without_a_hard_link_is_owner_only_on_posix(tmp_path, monkeypatch):
    _links_refused(monkeypatch)
    ss.ensure_key(tmp_path)
    assert stat.S_IMODE((tmp_path / ss.KEY_FILE).stat().st_mode) == 0o600


def _as_root(monkeypatch):
    chowned = []
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: chowned.append((os.path.basename(path), uid, gid)), raising=False)
    return chowned


# A root-owned 0600 key the service can't read would make every connector
# credential unusable.
def test_root_hands_a_new_key_to_the_data_dir_owner(tmp_path, monkeypatch):
    chowned = _as_root(monkeypatch)
    ss.ensure_key(tmp_path)
    owner = tmp_path.stat()
    assert chowned
    assert all(entry[1:] == (owner.st_uid, owner.st_gid) for entry in chowned)
    # The file that was linked into place carried the owner with it.
    assert any(name.startswith(ss.KEY_FILE + ".") and name.endswith(".tmp") for name, _, _ in chowned)


def test_root_hands_a_key_made_without_a_hard_link_to_the_data_dir_owner(tmp_path, monkeypatch):
    _links_refused(monkeypatch)
    chowned = _as_root(monkeypatch)
    ss.ensure_key(tmp_path)
    owner = tmp_path.stat()
    assert (ss.KEY_FILE, owner.st_uid, owner.st_gid) in chowned


def test_a_key_made_by_a_non_root_user_is_not_handed_over(tmp_path, monkeypatch):
    chowned = _as_root(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    ss.ensure_key(tmp_path)
    assert chowned == []


def test_without_hard_links_a_key_another_process_made_first_is_kept(tmp_path, monkeypatch):
    theirs = ss.Fernet.generate_key()
    _links_refused(monkeypatch, before_raising=lambda: (tmp_path / ss.KEY_FILE).write_bytes(theirs))
    assert ss.ensure_key(tmp_path) is False
    assert (tmp_path / ss.KEY_FILE).read_bytes() == theirs
    assert _leftover_temps(tmp_path) == []


@pytest.mark.parametrize("secret", ["pässword", "enc:v1:abc", "x" * 1000])
def test_encrypt_decrypt_round_trip(tmp_path, secret):
    token = ss.encrypt(tmp_path, secret)
    assert token.startswith("enc:v1:")
    assert secret not in token
    assert ss.decrypt(tmp_path, token) == secret


def test_decrypt_of_a_plain_value_returns_it(tmp_path):
    assert ss.decrypt(tmp_path, "hunter2") == "hunter2"


def test_decrypt_with_the_key_replaced_is_unreadable(tmp_path):
    token = ss.encrypt(tmp_path, "pw")
    _replace_key(tmp_path)
    result = ss.decrypt(tmp_path, token)
    assert isinstance(result, Unreadable)
    assert not result


def test_decrypt_with_no_key_file_is_unreadable_and_creates_nothing(tmp_path):
    token = ss.encrypt(tmp_path, "pw")
    (tmp_path / ss.KEY_FILE).unlink()
    ss._fernets.clear()
    assert isinstance(ss.decrypt(tmp_path, token), Unreadable)
    assert not (tmp_path / ss.KEY_FILE).exists()


def test_an_unusable_key_file_is_left_alone(tmp_path, caplog):
    (tmp_path / ss.KEY_FILE).write_bytes(b"junk")
    with caplog.at_level(logging.ERROR, logger="bacpack.security"):
        assert ss.ensure_key(tmp_path) is False
    assert (tmp_path / ss.KEY_FILE).read_bytes() == b"junk"
    assert "secret.key" in caplog.text
    with pytest.raises(SecretKeyProblem) as raised:
        ss.encrypt(tmp_path, "pw")
    assert "secret.key" in str(raised.value)


@pytest.fixture
def sessions(tmp_path):
    engine = make_engine(tmp_path / "x.db")
    init_db(engine)
    yield engine, sessionmaker(engine)
    engine.dispose()


def _connector(**fields):
    return Connector(name="c", url="http://x", **fields)


def test_connector_columns_store_ciphertext_and_load_plaintext(sessions):
    engine, factory = sessions
    with factory() as session:
        session.add(_connector(auth_type="basic", auth_password="pw", auth_token="tk", headers={"X-Key": "v", "Empty": ""}))
        session.commit()
    with engine.connect() as connection:
        password, token, headers = connection.execute(text("SELECT auth_password, auth_token, headers FROM connectors")).one()
    assert password.startswith("enc:v1:") and token.startswith("enc:v1:")
    stored = json.loads(headers)
    assert stored["X-Key"].startswith("enc:v1:") and stored["Empty"] == ""
    with factory() as session:
        connector = session.query(Connector).one()
        assert (connector.auth_password, connector.auth_token) == ("pw", "tk")
        assert connector.headers == {"X-Key": "v", "Empty": ""}


def test_unreadable_column_value_loads_as_unreadable_and_is_saved_back_unchanged(sessions, tmp_path):
    engine, factory = sessions
    with factory() as session:
        session.add(_connector(auth_password="pw", headers={"K": "v"}))
        session.commit()
    with engine.connect() as connection:
        before = connection.execute(text("SELECT auth_password, headers FROM connectors")).one()
    good = (tmp_path / ss.KEY_FILE).read_bytes()
    _replace_key(tmp_path)
    with factory() as session:
        connector = session.query(Connector).one()
        assert isinstance(connector.auth_password, Unreadable)
        assert isinstance(connector.headers["K"], Unreadable)
        assert connector.auth_password == "" and connector.headers == {"K": ""}
        connector.headers = {**connector.headers, "Other": "o"}
        connector.auth_token = "fresh"
        session.commit()
    with engine.connect() as connection:
        after = connection.execute(text("SELECT auth_password, headers FROM connectors")).one()
    assert after.auth_password == before.auth_password
    assert json.loads(after.headers)["K"] == json.loads(before.headers)["K"]
    (tmp_path / ss.KEY_FILE).write_bytes(good)
    ss._fernets.clear()
    with factory() as session:
        connector = session.query(Connector).one()
        assert connector.auth_password == "pw" and connector.headers["K"] == "v"


def test_two_engines_on_different_folders_use_their_own_keys(tmp_path):
    first = make_engine(tmp_path / "a" / "a.db")
    second = make_engine(tmp_path / "b" / "b.db")
    try:
        init_db(first)
        init_db(second)
        with sessionmaker(first)() as session:
            session.add(_connector(auth_password="pw"))
            session.commit()
        with sessionmaker(second)() as session:
            session.add(_connector(auth_password="other"))
            session.commit()
        with sessionmaker(first)() as session:
            assert session.query(Connector).one().auth_password == "pw"
        assert (tmp_path / "a" / ss.KEY_FILE).exists() and (tmp_path / "b" / ss.KEY_FILE).exists()
    finally:
        first.dispose()
        second.dispose()


def _raw_insert(engine, **columns):
    columns.setdefault("name", "c")
    defaults = {
        "type": "http-poll", "enabled": 1, "poll_interval_seconds": 30, "timeout_seconds": 10,
        "response_format": "json", "auth_type": "none", "verify_tls": 1, "rewrite_interval_seconds": 60,
    }
    columns = {**defaults, **columns}
    names = ", ".join(columns)
    marks = ", ".join(f":{name}" for name in columns)
    with engine.begin() as connection:
        connection.execute(text(f"INSERT INTO connectors (url, {names}) VALUES ('http://x', {marks})"), columns)


def _raw_row(engine):
    with engine.connect() as connection:
        return connection.execute(text("SELECT auth_password, auth_token, headers FROM connectors")).one()


def test_rewrite_encrypts_plain_password_token_and_header_values(sessions):
    engine, _ = sessions
    _raw_insert(engine, auth_password="pw", auth_token="tk", headers=json.dumps({"A": "x", "B": ""}))
    assert ss.rewrite_plaintext(engine) == (1, 0)
    password, token, headers = _raw_row(engine)
    assert password.startswith("enc:v1:") and token.startswith("enc:v1:")
    stored = json.loads(headers)
    assert stored["A"].startswith("enc:v1:") and stored["B"] == ""


def test_rewrite_twice_changes_nothing(sessions):
    engine, _ = sessions
    _raw_insert(engine, auth_password="pw", headers=json.dumps({"A": "x"}))
    ss.rewrite_plaintext(engine)
    before = _raw_row(engine)
    assert ss.rewrite_plaintext(engine) == (0, 0)
    assert _raw_row(engine) == before


def test_rewrite_leaves_an_undecryptable_value_alone_and_warns(sessions, tmp_path, caplog):
    engine, _ = sessions
    _raw_insert(engine, name="Secret Name", auth_password="pw")
    ss.rewrite_plaintext(engine)
    before = _raw_row(engine)
    _replace_key(tmp_path)
    with caplog.at_level(logging.WARNING, logger="bacpack.security"):
        assert ss.rewrite_plaintext(engine) == (0, 1)
    assert _raw_row(engine) == before
    assert "Secret Name" not in caplog.text and "pw" not in caplog.text.replace("password", "")


def test_init_db_makes_the_key_and_encrypts(tmp_path):
    engine = make_engine(tmp_path / "y.db")
    try:
        init_db(engine)
        assert (tmp_path / ss.KEY_FILE).exists()
        _raw_insert(engine, auth_password="pw")
        init_db(engine)
        assert _raw_row(engine)[0].startswith("enc:v1:")
    finally:
        engine.dispose()


def test_rewrite_with_an_unusable_key_leaves_plain_rows_plain_and_warns_once(sessions, tmp_path, caplog):
    engine, _ = sessions
    (tmp_path / ss.KEY_FILE).write_bytes(b"junk")
    ss._fernets.clear()
    _raw_insert(engine, auth_password="pw", headers=json.dumps({"A": "x"}))
    before = _raw_row(engine)
    with caplog.at_level(logging.WARNING, logger="bacpack.security"):
        assert ss.rewrite_plaintext(engine) == (0, 0)
    assert _raw_row(engine) == before
    warnings = [r for r in caplog.records if "plain text" in r.getMessage()]
    assert len(warnings) == 1 and "secret.key" in warnings[0].getMessage()
    assert "pw" not in caplog.text.replace("password", "")


def test_init_db_with_an_unusable_key_and_a_plain_row_still_starts(tmp_path):
    engine = make_engine(tmp_path / "z.db")
    try:
        init_db(engine)
        (tmp_path / ss.KEY_FILE).write_bytes(b"junk")
        ss._fernets.clear()
        _raw_insert(engine, auth_password="pw")
        init_db(engine)
        assert _raw_row(engine)[0] == "pw"
    finally:
        engine.dispose()


def test_rewrite_lets_a_non_busy_operational_error_propagate(tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError

    engine = make_engine(tmp_path / "ioerr.db")
    try:
        init_db(engine)
        _raw_insert(engine, auth_password="pw")

        def boom(*args, **kwargs):
            raise OperationalError("x", None, Exception("disk I/O error"))

        monkeypatch.setattr(ss, "_updates_for", boom)
        with pytest.raises(OperationalError):
            ss.rewrite_plaintext(engine)
    finally:
        engine.dispose()


def test_rewrite_under_a_busy_history_file_warns_and_retries_later(tmp_path, monkeypatch, caplog):
    import sqlite3

    from bacpack import db

    monkeypatch.setattr(db, "BUSY_TIMEOUT_SECONDS", 0.2)
    engine = make_engine(tmp_path / "busy.db")
    try:
        init_db(engine)
        _raw_insert(engine, auth_password="pw")
        before = _raw_row(engine)
        holder = sqlite3.connect(db.history_db_path(tmp_path / "busy.db"), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with caplog.at_level(logging.WARNING, logger="bacpack.security"):
                assert ss.rewrite_plaintext(engine) == (0, 0)
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        assert _raw_row(engine) == before
        assert "database is busy" in caplog.text
        assert "pw" not in caplog.text.replace("password", "")
        assert ss.rewrite_plaintext(engine) == (1, 0)
        assert _raw_row(engine)[0].startswith("enc:v1:")
    finally:
        engine.dispose()


def test_rewrite_reads_before_it_locks_and_takes_the_write_lock_first(sessions):
    """With nothing plain it never writes; otherwise the write lock is taken
    before the SELECT, so a busy file is waited for rather than failing."""
    from sqlalchemy import event

    engine, _ = sessions
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper() + " " + statement.strip().split()[1].upper())

    ss.rewrite_plaintext(engine)
    assert "BEGIN IMMEDIATE" not in statements and not any(s.startswith("UPDATE") for s in statements)

    _raw_insert(engine, auth_password="pw")
    statements.clear()
    ss.rewrite_plaintext(engine)
    first_select = statements.index("SELECT ID,")
    assert "BEGIN IMMEDIATE" in statements
    selects = [i for i, s in enumerate(statements) if s.startswith("SELECT")]
    assert statements.index("BEGIN IMMEDIATE") < selects[-1]
    assert first_select < statements.index("BEGIN IMMEDIATE")
