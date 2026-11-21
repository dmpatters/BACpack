import datetime as dt

from bacpack.readrange_cursors import MAX_CURSORS, ReadRangeCursors

STAMP = dt.datetime(2026, 1, 1, 12, 0, 0)
KEY = ("a", 1, True)


def test_get_returns_what_was_remembered_and_nothing_for_an_unknown_key():
    cursors = ReadRangeCursors()
    assert cursors.get(KEY) == ()
    cursors.remember(KEY, (STAMP, 7), None)
    assert cursors.get(KEY) == ((STAMP, 7),)


def test_a_new_end_becomes_the_newest_and_the_one_before_moves_to_second_place():
    cursors = ReadRangeCursors()
    cursors.remember(KEY, (STAMP, 1), None)
    cursors.remember(KEY, (STAMP, 2), 0)
    cursors.remember(KEY, (STAMP, 3), None)
    assert cursors.get(KEY) == ((STAMP, 3), (STAMP, 2))
    assert len(cursors) == 1


def test_a_retry_replaces_the_newest_and_keeps_the_second_place():
    cursors = ReadRangeCursors()
    cursors.remember(KEY, (STAMP, 1), None)
    cursors.remember(KEY, (STAMP, 2), 0)
    cursors.remember(KEY, (STAMP, 3), 1)
    assert cursors.get(KEY) == ((STAMP, 3), (STAMP, 1))


def test_directions_and_logs_are_separate_keys():
    cursors = ReadRangeCursors()
    cursors.remember(("a", 1, True), (STAMP, 1), None)
    assert cursors.get(("a", 1, False)) == ()
    assert cursors.get(("a", 2, True)) == ()
    assert cursors.get(("b", 1, True)) == ()


def test_the_least_recently_used_entry_is_evicted_at_the_bound():
    cursors = ReadRangeCursors()
    for client in range(MAX_CURSORS):
        cursors.remember((client, 1, True), (STAMP, client), None)
    cursors.get((0, 1, True))
    cursors.remember((MAX_CURSORS, 1, True), (STAMP, 0), None)
    assert len(cursors) == MAX_CURSORS
    assert cursors.get((0, 1, True))
    assert cursors.get((1, 1, True)) == ()


def test_the_bound_is_4096_entries():
    assert MAX_CURSORS == 4096
