"""BACpack: an open-source BACnet server (device simulator + gateway)."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version


def bacpack_version() -> str:
    """From installed package metadata, not hardcoded, so the API can never
    report a version other than the one deployed."""
    try:
        return _pkg_version("bacpack")
    except PackageNotFoundError:  # pragma: no cover - only when not pip-installed
        return "0.0.0-dev"
