"""Pull connectors (http-poll) and BACnet connectors, their read and write
mappings,
plus live connector status."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import connector_config
from ...config_schemas import (
    ConnectorCreate,
    ConnectorMappingCreate,
    ConnectorMappingUpdate,
    ConnectorUpdate,
    ConnectorWriteMappingCreate,
    ConnectorWriteMappingUpdate,
)
from ...connector_config import ConnectorRuleError
from ...connectors import ConnectorPoller, release_http_mappings
from ...models import BacnetObject, Connector, ConnectorMapping, ConnectorWriteMapping, Device
from ..deps import get_connector_poller, get_session

router = APIRouter()


# --- Pull connectors ----------------------


def _connector_dict(connector: Connector) -> dict:
    return {
        "id": connector.id,
        "name": connector.name,
        "type": connector.type,
        "enabled": connector.enabled,
        "url": connector.url,
        "poll_interval_seconds": connector.poll_interval_seconds,
        "timeout_seconds": connector.timeout_seconds,
        "response_format": connector.response_format,
        "auth_type": connector.auth_type,
        "auth_username": connector.auth_username,
        # Secrets are never echoed back: auth_password/auth_token are
        # omitted, and headers (where API keys go) are names only.
        "header_names": list((connector.headers or {}).keys()),
        "missing_credentials": _missing_credentials(connector),
        "unreadable_credentials": _unreadable_credentials(connector),
        "verify_tls": connector.verify_tls,
        "mapping_count": len(connector.mappings),
        # bacnet connectors
        "device_id": connector.device_id,
        "remote_device_instance": connector.remote_device_instance,
        "remote_address": connector.remote_address,
        "rewrite_interval_seconds": connector.rewrite_interval_seconds,
        "write_mapping_count": len(connector.write_mappings),
    }


def _missing_credentials(connector: Connector) -> list[str]:
    if connector.type != "http-poll":
        return []
    return connector_config.missing_credentials(
        auth_type=connector.auth_type,
        auth_password=connector.auth_password,
        auth_token=connector.auth_token,
        headers=connector.headers,
    )


def _unreadable_credentials(connector: Connector) -> list[str]:
    if connector.type != "http-poll":
        return []
    return connector_config.unreadable_credentials(
        auth_password=connector.auth_password, auth_token=connector.auth_token, headers=connector.headers
    )


def _header_values(sent: dict | None, stored: dict | None) -> dict | None:
    """A null value keeps the stored one (none stored: not set yet)."""
    if sent is None:
        return None
    stored = stored or {}
    return {name: stored.get(name, "") if value is None else value for name, value in sent.items()}


def _mapping_dict(mapping: ConnectorMapping) -> dict:
    return {
        "id": mapping.id,
        "connector_id": mapping.connector_id,
        "object_id": mapping.object_id,
        "json_path": mapping.json_path,
        "scale": mapping.scale,
        "offset": mapping.offset,
        "value_map": mapping.value_map,
        "remote_object_type": mapping.remote_object_type,
        "remote_object_instance": mapping.remote_object_instance,
        "remote_property": mapping.remote_property,
        "remote_array_index": mapping.remote_array_index,
        "mode": mapping.mode,
        "direction": mapping.direction,
    }


def _get_connector_or_404(session: Session, connector_id: int) -> Connector:
    connector = session.get(Connector, connector_id)
    if connector is None:
        raise HTTPException(status_code=404, detail=f"no connector with id {connector_id}")
    return connector


def _get_mapping_or_404(session: Session, connector_id: int, mapping_id: int) -> ConnectorMapping:
    mapping = session.get(ConnectorMapping, mapping_id)
    if mapping is None or mapping.connector_id != connector_id:
        raise HTTPException(status_code=404, detail=f"no mapping with id {mapping_id}")
    return mapping


_BACNET_ONLY_FIELDS = ("remote_device_instance", "remote_address", "rewrite_interval_seconds")
_REMOTE_POINT_FIELDS = ("remote_object_type", "remote_object_instance", "remote_property", "remote_array_index")


@contextmanager
def _rules():
    """connector_config's rules, as the HTTP error the API answers with."""
    try:
        yield
    except ConnectorRuleError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _point(mapping) -> tuple:
    return (
        mapping.remote_object_type,
        mapping.remote_object_instance,
        mapping.remote_property,
        mapping.remote_array_index,
    )


def _write_mapping_dict(mapping: ConnectorWriteMapping) -> dict:
    return {
        "id": mapping.id,
        "connector_id": mapping.connector_id,
        "object_id": mapping.object_id,
        "remote_object_type": mapping.remote_object_type,
        "remote_object_instance": mapping.remote_object_instance,
        "remote_property": mapping.remote_property,
        "remote_array_index": mapping.remote_array_index,
        "priority": mapping.priority,
    }


def _get_write_mapping_or_404(session: Session, connector_id: int, mapping_id: int) -> ConnectorWriteMapping:
    mapping = session.get(ConnectorWriteMapping, mapping_id)
    if mapping is None or mapping.connector_id != connector_id:
        raise HTTPException(status_code=404, detail=f"no write mapping with id {mapping_id}")
    return mapping


def _check_write_target_free(session: Session, connector_id: int, target: tuple, exclude_id=None) -> None:
    others = [
        (f"write mapping {other.id}", _point(other))
        for other in session.scalars(select(ConnectorWriteMapping).filter_by(connector_id=connector_id))
        if other.id != exclude_id
    ]
    with _rules():
        connector_config.check_write_target_free(target, others)


# --- Proxy mappings -----------------

_PROXY_FIELDS = ("direction", "scale", "offset", "value_map", *_REMOTE_POINT_FIELDS)


def _refuse_proxy_on_http(connector: Connector, direction) -> None:
    if direction == "proxy":
        with _rules():
            connector_config.check_proxy_connector(connector.type)


def _check_proxy(session: Session, connector: Connector, obj: BacnetObject, fields: dict) -> None:
    """A proxy mapping's rules. `fields` are the mapping's values after this
    create or edit (_PROXY_FIELDS)."""
    if fields["direction"] != "proxy":
        return
    with _rules():
        connector_config.check_proxy(connector.type, obj.object_type, fields)
    point = (fields["remote_object_type"], fields["remote_object_instance"], "present-value", None)
    _check_not_written(session, connector.remote_device_instance, point)


# The echo guard covers every BACnet connector pointed at the same remote
# device, on any local device: a write mapping anywhere on the point a proxy
# mirrors would send the mirrored value back.


def _connectors_at(session: Session, remote_device_instance) -> list:
    return session.scalars(
        select(Connector).filter_by(type="bacnet", remote_device_instance=remote_device_instance)
    ).all()


def _check_not_written(session: Session, remote_device_instance, point: tuple) -> None:
    """A proxy on `point`: no write mapping may write it."""
    writes = [
        (f"write mapping {write.id}", connector.name, _point(write))
        for connector in _connectors_at(session, remote_device_instance)
        for write in connector.write_mappings
    ]
    with _rules():
        connector_config.check_not_written(point, writes)


def _check_not_proxied(session: Session, remote_device_instance, target: tuple) -> None:
    """The other side of _check_proxy's echo guard: a write mapping onto a
    point a proxy mirrors."""
    proxies = [
        (
            f"read mapping {mapping.id}",
            connector.name,
            (mapping.remote_object_type, mapping.remote_object_instance, "present-value", None),
        )
        for connector in _connectors_at(session, remote_device_instance)
        for mapping in connector.mappings
        if mapping.direction == "proxy"
    ]
    with _rules():
        connector_config.check_not_proxied(target, proxies)


def _check_echo_free_at(session: Session, connector: Connector, remote_device_instance) -> None:
    """A connector about to point at another remote device: its proxies and
    write mappings mustn't meet the other side's there."""
    for mapping in connector.mappings:
        if mapping.direction == "proxy":
            point = (mapping.remote_object_type, mapping.remote_object_instance, "present-value", None)
            _check_not_written(session, remote_device_instance, point)
    for write in connector.write_mappings:
        target = (write.remote_object_type, write.remote_object_instance, write.remote_property, write.remote_array_index)
        _check_not_proxied(session, remote_device_instance, target)


@router.get("/connectors")
def list_connectors(session: Session = Depends(get_session)):
    connectors = session.scalars(select(Connector).order_by(Connector.id)).all()
    return [_connector_dict(c) for c in connectors]


@router.post("/connectors", status_code=201)
def create_connector(body: ConnectorCreate, session: Session = Depends(get_session)):
    if session.scalar(select(Connector).filter_by(name=body.name)) is not None:
        raise HTTPException(status_code=409, detail=f"connector named {body.name!r} already exists")
    if body.type == "bacnet":
        if session.get(Device, body.device_id) is None:
            raise HTTPException(status_code=422, detail=f"no device with id {body.device_id}")
        with _rules():
            connector_config.check_remote_instance(body.device_id, body.remote_device_instance)
        data = body.model_dump(exclude={"verify_tls"})
    else:
        # An http-poll connector has no device (a device_id would
        # cascade-delete it with that device) and no BACnet fields.
        data = body.model_dump(exclude={"device_id", *_BACNET_ONLY_FIELDS})
        data["headers"] = _header_values(body.headers, None)
    connector = Connector(**data)
    session.add(connector)
    session.commit()
    return _connector_dict(connector)


@router.get("/connectors/status")
def connectors_status(
    poller: ConnectorPoller = Depends(get_connector_poller),
    session: Session = Depends(get_session),
):
    """Live status, keyed by connector id. A bacnet connector's status is
    shown only while its device is running, so a stopped device's last
    status isn't passed off as live."""
    status = {connector_id: s.to_dict() for connector_id, s in poller.status.items()}
    for connector in session.scalars(select(Connector).filter_by(type="bacnet", enabled=True)):
        if (
            connector.runtime.client_status is not None
            and connector.device is not None
            and connector.device.runtime.process_status == "running"
        ):
            status[connector.id] = connector.runtime.client_status
    return status


@router.get("/connectors/{connector_id}")
def get_connector(connector_id: int, session: Session = Depends(get_session)):
    return _connector_dict(_get_connector_or_404(session, connector_id))


@router.post("/connectors/{connector_id}/preview")
async def preview_connector(
    connector_id: int,
    session: Session = Depends(get_session),
    poller: ConnectorPoller = Depends(get_connector_poller),
):
    """Fetch an http-poll connector's current response now, using its saved
    config, for the JSON path picker. Every upstream failure is a 200 with
    {"ok": false, "error": ...}; only a missing connector is a 404."""
    connector = _get_connector_or_404(session, connector_id)
    if connector.type == "bacnet":
        raise HTTPException(
            status_code=400, detail="preview is for http-poll connectors; browse a BACnet device instead"
        )
    return await poller.preview(connector_id)


@router.put("/connectors/{connector_id}")
def update_connector(
    connector_id: int, body: ConnectorUpdate, session: Session = Depends(get_session)
):
    connector = _get_connector_or_404(session, connector_id)
    updates = body.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"] != connector.name:
        clash = session.scalar(select(Connector).filter_by(name=updates["name"]))
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"connector named {updates['name']!r} already exists",
            )
    ignored = ("url", "verify_tls") if connector.type == "bacnet" else _BACNET_ONLY_FIELDS
    for field in ignored:
        updates.pop(field, None)
    if connector.type == "bacnet" and "remote_device_instance" in updates:
        with _rules():
            connector_config.check_remote_instance(connector.device_id, updates["remote_device_instance"])
        if updates["remote_device_instance"] != connector.remote_device_instance:
            _check_echo_free_at(session, connector, updates["remote_device_instance"])
    # A json connector needs a json_path on every mapping.
    if (
        connector.type == "http-poll"
        and updates.get("response_format") == "json"
        and connector.response_format != "json"
    ):
        missing = [str(m.id) for m in connector.mappings if not m.json_path]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{'mapping' if len(missing) == 1 else 'mappings'} {', '.join(missing)} "
                    f"{'has' if len(missing) == 1 else 'have'} no json_path; set one before switching to json"
                ),
            )
    if "headers" in updates:
        updates["headers"] = _header_values(updates["headers"], connector.headers)
    if connector.type == "http-poll" and connector.enabled and updates.get("enabled") is False:
        release_http_mappings(connector.mappings)
    for field, value in updates.items():
        setattr(connector, field, value)
    session.commit()
    return _connector_dict(connector)


@router.delete("/connectors/{connector_id}", status_code=204)
def delete_connector(connector_id: int, session: Session = Depends(get_session)):
    connector = _get_connector_or_404(session, connector_id)
    if connector.type == "http-poll":
        release_http_mappings(connector.mappings)
    session.delete(connector)
    session.commit()
    return Response(status_code=204)


@router.get("/connectors/{connector_id}/mappings")
def list_mappings(connector_id: int, session: Session = Depends(get_session)):
    connector = _get_connector_or_404(session, connector_id)
    return [_mapping_dict(m) for m in connector.mappings]


@router.post("/connectors/{connector_id}/mappings", status_code=201)
def create_mapping(
    connector_id: int, body: ConnectorMappingCreate, session: Session = Depends(get_session)
):
    connector = _get_connector_or_404(session, connector_id)
    target = session.get(BacnetObject, body.object_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"no object with id {body.object_id}")
    _refuse_proxy_on_http(connector, body.direction)
    label = f"object {body.object_id}"
    with _rules():
        connector_config.check_mapping_target(label, target.object_type, False)
    if session.scalar(select(ConnectorMapping).filter_by(object_id=body.object_id)) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"object {body.object_id} is already fed by another connector mapping",
        )
    with _rules():
        connector_config.check_mapping_target(label, target.object_type, bool(target.simulation))
    if connector.type == "bacnet":
        with _rules():
            connector_config.check_same_device(connector.device_id, label, target.device_id)
            connector_config.check_remote_point_given(body.remote_object_type, body.remote_object_instance)
        data = body.model_dump()
        data["json_path"] = None
        data["remote_property"] = data["remote_property"] or "present-value"
        _check_proxy(session, connector, target, data)
    else:
        with _rules():
            connector_config.check_json_path(connector.response_format, body.json_path)
        data = body.model_dump(exclude=set(_REMOTE_POINT_FIELDS))
    mapping = ConnectorMapping(connector=connector, **data)
    session.add(mapping)
    session.commit()
    return _mapping_dict(mapping)


@router.put("/connectors/{connector_id}/mappings/{mapping_id}")
def update_mapping(
    connector_id: int,
    mapping_id: int,
    body: ConnectorMappingUpdate,
    session: Session = Depends(get_session),
):
    mapping = _get_mapping_or_404(session, connector_id, mapping_id)
    updates = body.model_dump(exclude_unset=True)
    _refuse_proxy_on_http(mapping.connector, updates.get("direction"))
    ignored = ("json_path",) if mapping.connector.type == "bacnet" else _REMOTE_POINT_FIELDS
    for field in ignored:
        updates.pop(field, None)
    # As in create_mapping; json_path was popped above for a bacnet mapping.
    if "json_path" in updates:
        with _rules():
            connector_config.check_json_path(mapping.connector.response_format, updates["json_path"])
    fields = {f: updates[f] if f in updates else getattr(mapping, f) for f in _PROXY_FIELDS}
    _check_proxy(session, mapping.connector, mapping.object, fields)
    for field, value in updates.items():
        setattr(mapping, field, value)
    session.commit()
    return _mapping_dict(mapping)


@router.delete("/connectors/{connector_id}/mappings/{mapping_id}", status_code=204)
def delete_mapping(connector_id: int, mapping_id: int, session: Session = Depends(get_session)):
    mapping = _get_mapping_or_404(session, connector_id, mapping_id)
    if mapping.connector.type == "http-poll":
        release_http_mappings([mapping])
    session.delete(mapping)
    session.commit()
    return Response(status_code=204)


# --- BACnet write mappings ------------------
# The owning device's process sends each one on change and every
# rewrite_interval_seconds, and relinquishes the remote point when the
# mapping is deleted or moved.


@router.get("/connectors/{connector_id}/write-mappings")
def list_write_mappings(connector_id: int, session: Session = Depends(get_session)):
    connector = _get_connector_or_404(session, connector_id)
    return [_write_mapping_dict(m) for m in connector.write_mappings]


@router.post("/connectors/{connector_id}/write-mappings", status_code=201)
def create_write_mapping(
    connector_id: int, body: ConnectorWriteMappingCreate, session: Session = Depends(get_session)
):
    connector = _get_connector_or_404(session, connector_id)
    if connector.type != "bacnet":
        raise HTTPException(status_code=400, detail="write mappings are only for bacnet connectors")
    source = session.get(BacnetObject, body.object_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"no object with id {body.object_id}")
    with _rules():
        connector_config.check_same_device(connector.device_id, f"object {body.object_id}", source.device_id)
        connector_config.check_write_source(source.object_type)
    _check_write_target_free(
        session,
        connector_id,
        (body.remote_object_type, body.remote_object_instance, body.remote_property, body.remote_array_index),
    )
    _check_not_proxied(
        session,
        connector.remote_device_instance,
        (body.remote_object_type, body.remote_object_instance, body.remote_property, body.remote_array_index),
    )
    mapping = ConnectorWriteMapping(connector=connector, **body.model_dump())
    session.add(mapping)
    session.commit()
    return _write_mapping_dict(mapping)


@router.put("/connectors/{connector_id}/write-mappings/{mapping_id}")
def update_write_mapping(
    connector_id: int,
    mapping_id: int,
    body: ConnectorWriteMappingUpdate,
    session: Session = Depends(get_session),
):
    mapping = _get_write_mapping_or_404(session, connector_id, mapping_id)
    updates = body.model_dump(exclude_unset=True)
    target = tuple(updates.get(field, getattr(mapping, field)) for field in _REMOTE_POINT_FIELDS)
    _check_write_target_free(session, connector_id, target, exclude_id=mapping.id)
    _check_not_proxied(session, mapping.connector.remote_device_instance, target)
    for field, value in updates.items():
        setattr(mapping, field, value)
    session.commit()
    return _write_mapping_dict(mapping)


@router.delete("/connectors/{connector_id}/write-mappings/{mapping_id}", status_code=204)
def delete_write_mapping(connector_id: int, mapping_id: int, session: Session = Depends(get_session)):
    mapping = _get_write_mapping_or_404(session, connector_id, mapping_id)
    session.delete(mapping)
    session.commit()
    return Response(status_code=204)
