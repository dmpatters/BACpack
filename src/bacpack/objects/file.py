"""The config File object (file,1) every device carries for backup/restore.
Not a user object type (no DB row). Its contents are the BackupRestore
session's: backup snapshot, incoming restore buffer, or nothing.

bacpypes3 0.0.106's FileObject has no behaviour; the computed properties
below supply it.
"""
from __future__ import annotations

from bacpypes3.basetypes import DateTime
from bacpypes3.local.object import Object as LocalObjectMixin
from bacpypes3.object import FileObject
from bacpypes3.primitivedata import Boolean, Unsigned

from .. import timeutil
from ..backup_restore import CONFIG_FILE_INSTANCE, BackupRestore
from ..bacnet import WritePropertyAllowlisted
from .reserved_names import config_file_name


class ConfigFileObject(WritePropertyAllowlisted, LocalObjectMixin, FileObject):
    # Only File_Size (zeroing before a restore) is writable. LocalObjectMixin
    # supplies Property_List, which the protocol-only FileObject lacks.
    _WRITABLE_ATTRS = frozenset({"fileSize"})

    # A plain attribute, not an annotation: bacpypes3 builds an Object's
    # BACnet properties from its class annotations.
    _backup_restore = None  # a BackupRestore, set by build_config_file_object

    async def _post_init(self) -> None:
        # bacpypes3's local Object._post_init reads notificationClass, which
        # a File object doesn't have -- same no-op as the Calendar's.
        pass

    @property
    def fileSize(self) -> Unsigned:  # type: ignore[override]
        return Unsigned(self._backup_restore.size)

    @fileSize.setter
    def fileSize(self, value) -> None:
        # Some clients zero the file before a restore; truncate() only
        # allows 0, and only during performingARestore.
        self._backup_restore.truncate(int(value))

    # Getter-only: bacpypes3 answers a WriteProperty with writeAccessDenied.
    @property
    def readOnly(self) -> Boolean:  # type: ignore[override]
        return Boolean(self._backup_restore.state != "performingARestore")

    @property
    def modificationDate(self) -> DateTime:  # type: ignore[override]
        modified = self._backup_restore.modified
        return DateTime.now(modified) if modified is not None else timeutil.wildcard_datetime()


def build_config_file_object(device_name: str, backup_restore: BackupRestore) -> ConfigFileObject:
    obj = ConfigFileObject(
        objectIdentifier=("file", CONFIG_FILE_INSTANCE),
        objectName=config_file_name(device_name),
        description="Device configuration for BACnet backup/restore",
        fileType="application/json",
        archive=False,
        fileAccessMethod="streamAccess",
    )
    obj._backup_restore = backup_restore
    return obj
