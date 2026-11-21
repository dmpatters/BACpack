"""Device object construction."""

from __future__ import annotations

from bacpypes3.basetypes import BackupState, DeviceStatus, ObjectTypesSupported, TimeStamp
from bacpypes3.errors import PropertyError
from bacpypes3.local.device import DeviceObject
from bacpypes3.primitivedata import Boolean, CharacterString, Integer

from .. import bacpack_version, bacpypes3_compat, config_save, object_config
from ..backup_restore import (
    BACKUP_FAILURE_TIMEOUT,
    CONFIG_FILE_INSTANCE,
    RESTORE_COMPLETION_TIME,
)
from ..bacnet import normalized_attr
from ..capabilities import object_types_supported
from .. import device_clock, timeutil
from ..models import Device
from .reserved_names import find_reserved_name_clash

# Only vendorName is per-device; the rest identify BACpack. Without them
# bacpypes3's defaults leak through ("BACpypes", "N/A", "1.0"). The version is
# bacpack_version(), the one /api/version reports.
DEFAULT_VENDOR_NAME = "BACpack"
_MODEL_NAME = "BACpack Simulator"


class BacpackDeviceObject(DeviceObject):
    """Reports the object types BACpack builds; upstream hard-codes an empty
    Protocol_Object_Types_Supported.

    Computed properties are getter-only on purpose: a write then gets
    writeAccessDenied instead of a Simple-ACK for a dropped value.
    """

    @property
    def protocolObjectTypesSupported(self) -> ObjectTypesSupported:  # type: ignore[override]
        return ObjectTypesSupported(object_types_supported())

    # Computed from the host zone. Local_Date/
    # Local_Time use Date.now()/Time.now(), which device_clock.install() shifts.
    @property
    def utcOffset(self) -> Integer:  # type: ignore[override]
        return Integer(device_clock.utc_offset_minutes())

    @property
    def daylightSavingsStatus(self) -> Boolean:  # type: ignore[override]
        return Boolean(device_clock.is_dst())

    # Live backup/restore session, set by build_application.
    _backup_restore = None

    @property
    def backupAndRestoreState(self) -> BackupState:  # type: ignore[override]
        state = self._backup_restore.state if self._backup_restore is not None else "idle"
        return BackupState(state)

    # A client that predates Backup_And_Restore_State sees a backup or
    # restore only through System_Status. The setter is only for bacpypes3's
    # construction default; a BACnet write never reaches it (write_property's
    # allowlist refuses it first).
    _system_status = DeviceStatus.operational

    @property
    def systemStatus(self) -> DeviceStatus:  # type: ignore[override]
        if self._backup_restore is not None and self._backup_restore.state != "idle":
            return DeviceStatus(self._backup_restore.system_status)
        return self._system_status

    @systemStatus.setter
    def systemStatus(self, value) -> None:
        self._system_status = DeviceStatus(value)

    # Writable and saved to the device row; Backup_Failure_Timeout is for the
    # backup client to set. Everything else is refused: it wouldn't be saved,
    # and an Object_Identifier write would move the device (breaking restore,
    # reinitialize and time sync).
    _WRITABLE_ATTRS = frozenset({"objectName", "description", "location", "backupFailureTimeout"})
    _DEVICE_COLUMNS = {
        "objectName": "name",
        "description": "description",
        "location": "location",
        "backupFailureTimeout": "backup_failure_timeout",
    }

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        # normalized_attr: off the wire, attr is a PropertyIdentifier, not a name.
        name = normalized_attr(self, attr)
        if name not in self._WRITABLE_ATTRS:
            raise PropertyError("writeAccessDenied")
        if name == "backupFailureTimeout":
            saved = int(value)
            # Unsigned16 in the standard; 0 would end every session at once.
            if not 1 <= saved <= 65535:
                raise PropertyError("value-out-of-range")
        else:
            saved = str(value)
        if name == "objectName":
            # Names never start or end with whitespace, on any save path.
            saved = saved.strip()
            value = CharacterString(saved)
            self._check_new_name(saved)
        await super().write_property(attr, value, index, priority)
        if name == "backupFailureTimeout" and self._backup_restore is not None:
            # Takes effect from the session's next request.
            self._backup_restore.timeout = saved
        await self._save_device_column(self._DEVICE_COLUMNS[name], saved)

    def _check_new_name(self, new_name: str) -> None:
        if not new_name.strip():
            raise PropertyError("value-out-of-range")
        app = bacpypes3_compat.object_app(self)
        if app is None:
            return
        others = [obj for obj in app.objectIdentifier.values() if obj is not self]
        # BACnet names are unique within a device, the Device object included.
        if new_name in {str(obj.objectName) for obj in others}:
            raise PropertyError("duplicate-name")
        # file,1 is named after the device, so a clash with its name would
        # stop the device starting. Trend logs and the network port are
        # passed as plain names; the reserved-name objects are left out.
        user_names = [
            {"name": str(obj.objectName), "trended": False}
            for obj in others
            if str(obj.objectIdentifier[0]) not in ("file", "network-port")
        ]
        if find_reserved_name_clash(new_name, user_names) is not None:
            raise PropertyError("duplicate-name")
        # Saved rows too: the UI may have saved one this process hasn't
        # loaded.
        try:
            object_config.check_saved_device_name(
                getattr(app, "session_factory", None), int(self.objectIdentifier[1]), new_name
            )
        except ValueError as exc:
            raise PropertyError("duplicate-name") from exc

    async def _save_device_column(self, column: str, plain) -> None:
        session_factory = getattr(bacpypes3_compat.object_app(self), "session_factory", None)
        if session_factory is None:
            return
        device_id = int(self.objectIdentifier[1])
        await config_save.save_columns(
            session_factory, Device, device_id, {column: plain}, f"device {column} for device {device_id}"
        )

    # bacpypes3's own computed properties, re-declared getter-only: upstream
    # gives each an ignored setter. Local_Date/Local_Time change only through
    # TimeSynchronization, never WriteProperty.
    localDate = property(DeviceObject.localDate.fget)
    localTime = property(DeviceObject.localTime.fget)
    objectList = property(DeviceObject.objectList.fget)
    protocolServicesSupported = property(DeviceObject.protocolServicesSupported.fget)
    deviceAddressBinding = property(DeviceObject.deviceAddressBinding.fget)
    activeCovSubscriptions = property(DeviceObject.activeCovSubscriptions.fget)


def _backup_failure_timeout(device: Device) -> int:
    # `or`: a bare unsaved Device reads None, as in tests.
    return device.backup_failure_timeout or BACKUP_FAILURE_TIMEOUT


def build_device_object(device: Device) -> BacpackDeviceObject:
    version = bacpack_version()
    return BacpackDeviceObject(
        objectIdentifier=("device", device.id),
        objectName=device.name,
        description=device.description or "",
        # Optional in ASHRAE 135, but always present: some software
        # reads it with the device header.
        location=device.location or "",
        vendorIdentifier=device.vendor_identifier,
        vendorName=device.vendor_name or DEFAULT_VENDOR_NAME,
        modelName=_MODEL_NAME,
        firmwareRevision=version,
        applicationSoftwareVersion=version,
        # Static for the process: a restore always respawns the device.
        # `or 1` / `is None`: an unsaved Device (tests) reads None.
        databaseRevision=device.database_revision or 1,
        lastRestoreTime=(
            TimeStamp.as_dateTime(timeutil.from_db(device.last_restore_time).timestamp())
            if device.last_restore_time is not None
            else TimeStamp(dateTime=timeutil.wildcard_datetime())
        ),
        configurationFiles=[("file", CONFIG_FILE_INSTANCE)],
        backupFailureTimeout=_backup_failure_timeout(device),
        backupPreparationTime=0,
        restorePreparationTime=0,
        restoreCompletionTime=RESTORE_COMPLETION_TIME,
    )
