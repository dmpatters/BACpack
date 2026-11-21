"""Calendar object class. bacpypes3 has no local Calendar class, so this one
is built from scratch; the saved entry form is in `calendar_config.py`.
"""

from __future__ import annotations

from bacpypes3.errors import PropertyError
from bacpypes3.local.object import Object as LocalObjectMixin
from bacpypes3.local.schedule import ScheduleObject as LocalScheduleObject
from bacpypes3.local.schedule import date_in_calendar_entry
from bacpypes3.object import CalendarObject as _CalendarObjectBase
from bacpypes3.primitivedata import Boolean, Date

from .. import bacpypes3_compat, config_save, object_config
from ..bacnet import WritePropertyAllowlisted, normalized_attr
from ..models import BacnetObject
from .calendar_config import unbuild_calendar_entry, validate_calendar_config
from .schedule import reinterpret_schedule

_CALENDAR_WRITABLE_ATTRS = frozenset({"dateList"})


class WritePropertyAllowlistedCalendar(WritePropertyAllowlisted):
    @property
    def _WRITABLE_ATTRS(self) -> frozenset:  # type: ignore[override]
        return _CALENDAR_WRITABLE_ATTRS if getattr(self, "_bacnet_writable", False) else frozenset()


class CalendarConfigPersisted:
    """ScheduleConfigPersisted's write-back, for dateList."""

    async def write_property(self, attr, value, index=None, priority=None) -> None:
        if normalized_attr(self, attr) != "dateList":
            await super().write_property(attr, value, index, priority)
            return

        try:
            date_list = [unbuild_calendar_entry(entry) for entry in value] if value else None
            validate_calendar_config(date_list)
        except (ValueError, TypeError, AttributeError) as exc:
            raise PropertyError("value-out-of-range") from exc
        try:
            object_config.check_saved_row(
                getattr(self, "_calendar_session_factory", None),
                getattr(self, "_calendar_object_id", None),
                {"date_list": date_list},
            )
        except ValueError as exc:
            raise PropertyError("value-out-of-range") from exc

        await super().write_property(attr, value, index, priority)

        session_factory = getattr(self, "_calendar_session_factory", None)
        object_id = getattr(self, "_calendar_object_id", None)
        if session_factory is not None and object_id is not None:
            await config_save.save_columns(
                session_factory, BacnetObject, object_id, {"date_list": date_list},
                f"date_list for object id {object_id}",
            )
        self._reevaluate_referencing_schedules()

    def _reevaluate_referencing_schedules(self) -> None:
        """Schedules referencing this calendar re-evaluate now, not at their
        next transition; their change hooks then command the targets."""
        app = bacpypes3_compat.object_app(self)
        if app is None:
            return
        own = (str(self.objectIdentifier[0]), int(self.objectIdentifier[1]))
        for obj in list(app.objectIdentifier.values()):
            if not isinstance(obj, LocalScheduleObject):
                continue
            for event in obj.exceptionSchedule or []:
                ref = event.period.calendarReference
                if ref is not None and (str(ref[0]), int(ref[1])) == own:
                    reinterpret_schedule(obj)
                    break


class LocalCalendarObject(
    WritePropertyAllowlistedCalendar, CalendarConfigPersisted, LocalObjectMixin, _CalendarObjectBase
):
    """presentValue is a live @property computed from dateList and today's
    date on every read, never stored."""

    async def _post_init(self) -> None:
        # Upstream _post_init reads self.notificationClass, which Calendar
        # doesn't have: AttributeError in a fire-and-forget task.
        pass

    @property
    def presentValue(self) -> Boolean:
        today = _current_bacnet_date(bacpypes3_compat.object_app(self))
        for entry in self.dateList or []:
            if date_in_calendar_entry(today, entry):
                return Boolean(True)
        return Boolean(False)


def _current_bacnet_date(app) -> Date:
    """Today's date from the device object's localDate (as schedule
    evaluation uses) over the system clock, so the device clock applies."""
    if app is not None and getattr(app, "device_object", None) is not None:
        local_date = app.device_object.localDate
        if local_date is not None:
            return local_date
    return Date.now()
