from bacpack import gc_freeze


def _refreezer():
    calls = []
    return calls, gc_freeze.Refreezer(
        collect=lambda: calls.append("collect"),
        freeze=lambda: calls.append("freeze"),
        unfreeze=lambda: calls.append("unfreeze"),
    )


def test_below_the_threshold_nothing_is_called():
    calls, refreezer = _refreezer()
    assert refreezer.changed(99, 50) is False
    assert calls == []
    assert refreezer.pending == 99


def test_reaching_the_floor_refreezes_in_order():
    calls, refreezer = _refreezer()
    refreezer.changed(99, 50)
    assert refreezer.changed(1, 50) is True
    assert calls == ["unfreeze", "collect", "freeze"]
    assert refreezer.pending == 0


def test_a_big_device_uses_a_tenth_of_its_objects():
    _, refreezer = _refreezer()
    assert refreezer.changed(499, 5000) is False
    assert refreezer.changed(1, 5000) is True


def test_the_threshold_follows_the_current_size():
    _, refreezer = _refreezer()
    assert refreezer.changed(150, 5000) is False
    assert refreezer.changed(1, 1000) is True


def test_one_big_swap_refreezes_at_once():
    calls, refreezer = _refreezer()
    assert refreezer.changed(5000, 5000) is True
    assert calls == ["unfreeze", "collect", "freeze"]


def test_freeze_startup_collects_then_freezes(monkeypatch):
    calls = []
    monkeypatch.setattr(gc_freeze.gc, "collect", lambda *a: calls.append("collect"))
    monkeypatch.setattr(gc_freeze.gc, "freeze", lambda: calls.append("freeze"))
    gc_freeze.freeze_startup()
    assert calls == ["collect", "freeze"]
