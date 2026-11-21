from __future__ import annotations

import json

import pytest

from bacpack.config_io import apply_device_restore, parse_config_document
from bacpack.config_schemas import ConfigImportError
from bacpack.csv_io import CsvImportError, read_objects_csv
from bacpack.unicode_text import has_unpaired_surrogate

LONE = json.dumps("\ud800")  # the JSON text of a lone surrogate


def test_has_unpaired_surrogate_looks_through_keys_values_and_lists():
    assert has_unpaired_surrogate("a\ud800")
    assert has_unpaired_surrogate({"k": ["x", {"y\udfff": 1}]})
    assert has_unpaired_surrogate({"k": ("x", "\ud800")})
    assert not has_unpaired_surrogate({"k": ["smile \U0001f600", 1, None]})
    assert not has_unpaired_surrogate(json.loads(json.dumps("\U0001f600")))  # dumps writes an escaped pair


def test_a_config_document_with_a_surrogate_is_a_config_import_error():
    doc = json.loads('{"devices": [{"id": 1, "name": %s, "objects": []}]}' % LONE)
    with pytest.raises(ConfigImportError, match="surrogate"):
        parse_config_document(doc)


def test_a_restore_file_with_a_surrogate_is_a_config_import_error(session_factory):
    from bacpack.models import Device

    with session_factory() as session:
        session.add(Device(id=1, name="D"))
        session.commit()
        data = ('{"devices": [{"id": 1, "name": %s, "objects": []}]}' % LONE).encode()
        with pytest.raises(ConfigImportError, match="surrogate"):
            apply_device_restore(session, session.get(Device, 1), data)


def test_a_csv_json_cell_with_a_surrogate_is_an_import_error_with_its_row():
    header = "object_type,instance_number,name,weekly_schedule\n"
    row = 'schedule,1,S,"[[%s]]"\n' % LONE.replace('"', '""')
    with pytest.raises(CsvImportError, match="row 2"):
        read_objects_csv(header + row)
