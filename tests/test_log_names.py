from bacpack import log_names
from bacpack.diagnostics_standins import StandIns


def test_the_three_forms():
    assert log_names.object_ref("analog-value", 9, "Old Chiller") == 'analog-value:9 "Old Chiller"'
    assert log_names.device_ref(599001, "AHU-1") == 'device 599001 "AHU-1"'
    assert log_names.connector_ref(3, "Weather feed") == 'connector 3 "Weather feed"'


def test_backslash_and_quote_are_escaped_and_round_trip():
    name = 'a "b" \\ c'
    assert log_names.quote(name) == '"a \\"b\\" \\\\ c"'
    assert log_names.unescape_name(log_names.quote(name)[1:-1]) == name


def test_the_diagnostics_scan_reads_back_what_the_helper_writes():
    table = StandIns()
    table.name("analog-value", 'Chiller "A"')
    line = f"changed {log_names.object_ref('analog-value', 4, 'Chiller \"A\"')} and {log_names.object_ref('analog-value', 5, 'Renamed \\ away')}"
    assert table.scan(line) == 'changed analog-value:4 "analog-value-1" and analog-value:5 "<name>"'


def test_live_object_ref_uses_identifier_and_name():
    class Obj:
        objectIdentifier = ("binary-value", 2)
        objectName = "Fan"

    assert log_names.live_object_ref(Obj()) == 'binary-value:2 "Fan"'


def test_the_log_sites_that_name_objects_use_the_format():
    from bacpack import live_config, live_swap

    class Obj:
        objectIdentifier = ("analog-value", 9)
        objectName = 'Old "Chiller"'

    assert live_swap._label(Obj()) == 'analog-value:9 "Old \\"Chiller\\""'  # pyright: ignore[reportPrivateUsage]
    row = {"object_type": "binary-value", "instance_number": 2, "name": "Fan"}
    assert live_config._row_ref(row) == 'binary-value:2 "Fan"'  # pyright: ignore[reportPrivateUsage]
    assert live_config._object_error(row, ValueError("x")) == 'object binary-value:2 "Fan": x'  # pyright: ignore[reportPrivateUsage]
