"""Asking the Windows Service Control Manager to stop BACpack's own service.
install.ps1 grants the service account stop and
query status (LCWP) on that one service; nothing else is opened."""
from __future__ import annotations

import sys

# WinSW 2.12 puts the service id in its child's environment.
SERVICE_ID_ENV = "SERVICE_ID"

_SC_MANAGER_CONNECT = 0x0001
_SERVICE_STOP = 0x0020
_SERVICE_CONTROL_STOP = 1
_SERVICE_STATUS_SIZE = 28


def stop_own_service(name: str) -> str | None:
    """Request a stop of service `name`; None once the request is accepted,
    else the reason it was refused."""
    if sys.platform != "win32":
        return "not on Windows"
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenSCManagerW.restype = wintypes.HANDLE
    advapi.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    advapi.OpenServiceW.restype = wintypes.HANDLE
    advapi.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    advapi.ControlService.restype = wintypes.BOOL
    advapi.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    advapi.CloseServiceHandle.restype = wintypes.BOOL
    advapi.CloseServiceHandle.argtypes = [wintypes.HANDLE]

    manager = advapi.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if not manager:
        return f"opening the service manager failed, Windows error {ctypes.get_last_error()}"
    try:
        service = advapi.OpenServiceW(manager, name, _SERVICE_STOP)
        if not service:
            return f"opening the service failed, Windows error {ctypes.get_last_error()}"
        try:
            status = ctypes.create_string_buffer(_SERVICE_STATUS_SIZE)
            if not advapi.ControlService(service, _SERVICE_CONTROL_STOP, status):
                return f"the stop request failed, Windows error {ctypes.get_last_error()}"
        finally:
            advapi.CloseServiceHandle(service)
    finally:
        advapi.CloseServiceHandle(manager)
    return None
