"""BackupRestore controller -- the DM-BR-B session state machine and the
config file's bytes."""
from __future__ import annotations

import asyncio

import pytest
from bacpypes3.errors import ExecutionError

from bacpack.backup_restore import MAX_RESTORE_BYTES, BackupRestore


def _code(excinfo) -> str:
    return str(excinfo.value.errorCode)


def test_starts_idle_and_empty():
    br = BackupRestore()
    assert br.state == "idle"
    assert br.size == 0
    assert br.read(0, 100) == (b"", True)


def test_backup_round_trip():
    br = BackupRestore()
    br.start_backup(b"hello world")
    assert br.state == "performingABackup"
    assert br.size == 11
    assert br.modified is not None
    br.end_backup()
    assert br.state == "idle"
    assert br.size == 0


@pytest.mark.parametrize(
    "setup, action",
    [
        (lambda br: br.start_restore(), lambda br: br.start_backup(b"x")),
        (lambda br: br.start_backup(b"x"), lambda br: br.start_restore()),
        (lambda br: None, lambda br: br.end_backup()),
        (lambda br: None, lambda br: br.abort_restore()),
        (lambda br: None, lambda br: br.take_restore_data()),
        (lambda br: br.start_backup(b"x"), lambda br: br.take_restore_data()),
    ],
)
def test_wrong_state_is_configuration_in_progress(setup, action):
    br = BackupRestore()
    setup(br)
    with pytest.raises(ExecutionError) as excinfo:
        action(br)
    assert _code(excinfo) == "configurationInProgress"


def test_read_slices_and_reports_end_of_file():
    br = BackupRestore()
    br.start_backup(b"0123456789")
    assert br.read(0, 4) == (b"0123", False)
    assert br.read(4, 6) == (b"456789", True)
    assert br.read(8, 100) == (b"89", True)
    assert br.read(10, 5) == (b"", True)


@pytest.mark.parametrize("start", [-1, 11])
def test_read_bad_position(start):
    br = BackupRestore()
    br.start_backup(b"0123456789")
    with pytest.raises(ExecutionError) as excinfo:
        br.read(start, 1)
    assert _code(excinfo) == "invalidFileStartPosition"


def test_write_outside_a_restore_is_denied():
    br = BackupRestore()
    with pytest.raises(ExecutionError) as excinfo:
        br.write(0, b"x")
    assert _code(excinfo) == "fileAccessDenied"
    br.start_backup(b"abc")
    with pytest.raises(ExecutionError):
        br.write(0, b"x")


def test_write_append_overwrite_and_extend():
    br = BackupRestore()
    br.start_restore()
    assert br.write(-1, b"hello") == 0
    assert br.write(-1, b" world") == 5
    assert br.write(0, b"J") == 0
    assert br.write(6, b"there!") == 6
    assert br.take_restore_data() == b"Jello there!"
    assert br.state == "idle"


def test_write_gap_is_rejected():
    br = BackupRestore()
    br.start_restore()
    br.write(-1, b"abc")
    with pytest.raises(ExecutionError) as excinfo:
        br.write(5, b"x")
    assert _code(excinfo) == "invalidFileStartPosition"
    with pytest.raises(ExecutionError):
        br.write(-2, b"x")


def test_write_past_the_cap_is_file_full():
    br = BackupRestore()
    br.start_restore()
    br.write(-1, b"x" * MAX_RESTORE_BYTES)
    with pytest.raises(ExecutionError) as excinfo:
        br.write(-1, b"y")
    assert _code(excinfo) == "fileFull"
    assert br.size == MAX_RESTORE_BYTES


def test_truncate_to_zero_during_restore():
    br = BackupRestore()
    br.start_restore()
    br.write(-1, b"abc")
    br.truncate(0)
    assert br.size == 0


def test_truncate_to_non_zero_is_out_of_range():
    br = BackupRestore()
    br.start_restore()
    with pytest.raises(ExecutionError) as excinfo:
        br.truncate(5)
    assert _code(excinfo) == "valueOutOfRange"


def test_truncate_outside_a_restore_is_denied():
    br = BackupRestore()
    with pytest.raises(ExecutionError) as excinfo:
        br.truncate(0)
    assert _code(excinfo) == "writeAccessDenied"


def test_abort_restore_drops_the_buffer():
    br = BackupRestore()
    br.start_restore()
    br.write(-1, b"abc")
    br.abort_restore()
    assert br.state == "idle"
    assert br.size == 0


@pytest.mark.asyncio
async def test_session_times_out_back_to_idle():
    br = BackupRestore(timeout=0.05)
    br.start_backup(b"abc")
    for _ in range(100):
        if br.state == "idle":
            break
        await asyncio.sleep(0.01)
    assert br.state == "idle"
    assert br.size == 0


@pytest.mark.asyncio
async def test_activity_restarts_the_timeout():
    # Read off the timer handles, not raced against the wall clock: a loaded
    # machine stalls longer than any margin a short test can afford.
    br = BackupRestore(timeout=60)
    br.start_restore()
    for _ in range(3):
        before = br._timer
        await asyncio.sleep(0.01)
        br.write(-1, b"x")
        assert before.cancelled()
        assert br._timer is not before and br._timer.when() > before.when()
    assert br.state == "performingARestore"
    br.reset()


@pytest.mark.asyncio
async def test_reset_cancels_the_timer():
    br = BackupRestore(timeout=0.05)
    br.start_backup(b"abc")
    br.reset()
    br.start_restore()
    br._timer.cancel()  # isolate: only the first session's timer could fire now
    await asyncio.sleep(0.1)
    assert br.state == "performingARestore"


# --- A refusal says why (YABE backup/restore pass follow-ups) ---------------


def _reason(excinfo) -> str:
    return excinfo.value.reason


@pytest.mark.parametrize(
    "setup, action, reason",
    [
        (lambda br: br.start_restore(), lambda br: br.start_backup(b"x"), "a restore is still in progress"),
        (lambda br: br.start_backup(b"x"), lambda br: br.start_restore(), "a backup is still in progress"),
        (lambda br: None, lambda br: br.end_backup(), "no backup is in progress"),
        (lambda br: br.start_restore(), lambda br: br.end_backup(), "a restore is still in progress"),
        (lambda br: None, lambda br: br.abort_restore(), "no restore is in progress"),
        (lambda br: None, lambda br: br.take_restore_data(), "no restore is in progress"),
        (lambda br: br.start_backup(b"x"), lambda br: br.take_restore_data(), "a backup is still in progress"),
        (lambda br: None, lambda br: br.write(0, b"x"), "no restore is in progress"),
        (lambda br: br.start_backup(b"x"), lambda br: br.write(0, b"x"), "a backup is still in progress"),
    ],
)
def test_a_wrong_state_refusal_says_why(setup, action, reason):
    br = BackupRestore()
    setup(br)
    with pytest.raises(ExecutionError) as excinfo:
        action(br)
    assert _reason(excinfo) == reason


def test_require_is_the_shared_state_check():
    br = BackupRestore()
    br.require("idle")  # no error
    br.start_backup(b"x")
    with pytest.raises(ExecutionError) as excinfo:
        br.require("idle")
    assert _code(excinfo) == "configurationInProgress"
    assert _reason(excinfo) == "a backup is still in progress"


@pytest.mark.parametrize(
    "setup, action, reason",
    [
        (lambda br: br.start_backup(b"0123456789"), lambda br: br.read(11, 1), "start position 11 is outside the file (10 octets)"),
        (lambda br: br.start_restore(), lambda br: br.write(5, b"x"), "start position 5 is past the end of the data written so far (0 octets)"),
    ],
)
def test_a_bad_position_refusal_says_why(setup, action, reason):
    br = BackupRestore()
    setup(br)
    with pytest.raises(ExecutionError) as excinfo:
        action(br)
    assert _reason(excinfo) == reason


def test_a_file_full_refusal_says_why():
    br = BackupRestore()
    br.start_restore()
    with pytest.raises(ExecutionError) as excinfo:
        br.write(0, b"x" * (MAX_RESTORE_BYTES + 1))
    assert _reason(excinfo) == "the restore file would be larger than 64 MiB"


def test_a_big_devices_restore_file_fits():
    # A 5,000-object device's backup is about 8.9 MB.
    br = BackupRestore()
    br.start_restore()
    br.write(0, b"x" * (20 * 1024 * 1024))
    assert br.size == 20 * 1024 * 1024


@pytest.mark.asyncio
async def test_a_refusal_after_a_timeout_names_the_timeout():
    br = BackupRestore(timeout=0.05)
    br.start_backup(b"abc")
    for _ in range(100):
        if br.state == "idle":
            break
        await asyncio.sleep(0.01)
    with pytest.raises(ExecutionError) as excinfo:
        br.end_backup()
    assert _reason(excinfo) == (
        "no backup is in progress (the last backup timed out after 0.05 s "
        "without activity, Backup_Failure_Timeout)"
    )


@pytest.mark.asyncio
async def test_a_new_session_forgets_the_timeout():
    br = BackupRestore(timeout=0.05)
    br.start_backup(b"abc")
    for _ in range(100):
        if br.state == "idle":
            break
        await asyncio.sleep(0.01)
    br.start_backup(b"abc")
    br.end_backup()
    with pytest.raises(ExecutionError) as excinfo:
        br.end_backup()
    assert _reason(excinfo) == "no backup is in progress"


def test_backup_carries_binary_text():
    import json

    from bacpack.config_io import export_device_backup, parse_config
    from bacpack.models import BacnetObject, Device

    device = Device(id=1, name="Dev-1")
    device.objects.append(
        BacnetObject(
            object_type="binary-value", instance_number=1, name="BV-1", active_text="Running", inactive_text="Stopped"
        )
    )
    [parsed] = parse_config(json.loads(export_device_backup(device)))
    [row] = parsed["objects"]
    assert (row["active_text"], row["inactive_text"]) == ("Running", "Stopped")


def test_backup_carries_start_value():
    import json

    from bacpack.config_io import export_device_backup, parse_config
    from bacpack.models import BacnetObject, Device

    device = Device(id=1, name="Dev-1")
    device.objects.append(BacnetObject(object_type="binary-input", instance_number=1, name="BI-1", start_value="active"))
    [row] = parse_config(json.loads(export_device_backup(device)))[0]["objects"]
    assert row["start_value"] == "active"


def test_restore_keeps_fixed_trend_log_identity():
    import json

    from bacpack.config_io import export_device_backup, parse_config
    from bacpack.models import BacnetObject, Device

    device = Device(id=1, name="Dev-1")
    device.objects.append(
        BacnetObject(
            object_type="analog-input", instance_number=1, name="AI-1", trended=True,
            trend_log_instance=77, trend_log_name="Zone Temp Log",
        )
    )
    [row] = parse_config(json.loads(export_device_backup(device)))[0]["objects"]
    assert (row["trend_log_instance"], row["trend_log_name"]) == (77, "Zone Temp Log")
