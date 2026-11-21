"""Fetch, verify, unpack and build the pinned bacnet-stack C tools.
Every failure is an InteropSetupError with its reason: under --interop a
missing prerequisite is never a skip."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Callable

VERSION = "1.7.0"
# Tag bacnet-stack-1.7.0, commit 03acade70f4586b4f9aacd77c13d63f6192a021c.
URL = "https://github.com/bacnet-stack/bacnet-stack/archive/refs/tags/bacnet-stack-1.7.0.tar.gz"
SHA256 = "9EBD5DB0F1850BE575849011E50C8A9750D2827A1E40CBA89836C006D12E2629"
TARBALL = f"bacnet-stack-{VERSION}.tar.gz"
TOP_FOLDER = f"bacnet-stack-bacnet-stack-{VERSION}"
TREE = f"bacnet-stack-{VERSION}"
TOOLS = (
    "bacwi", "bacrp", "bacrpm", "bacwp", "bacwpm", "bacscov", "bacrr", "bacge",
    "bacackalarm", "bacdcc", "bacrd", "bacwh", "bacts", "bacepics",
)
BUILD_TIMEOUT = 600


class InteropSetupError(Exception):
    pass


def cache_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "bacpack-interop"
    return Path.home() / ".cache" / "bacpack-interop"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_tarball(path: Path, *, advice: str = "") -> None:
    actual = sha256_of(path)
    if actual.lower() != SHA256.lower():
        raise InteropSetupError(f"{path} has SHA-256 {actual.upper()}, expected {SHA256}{advice}")


def _urllib_download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)


def ensure_tarball(cache: Path, *, download: Callable[[str, Path], None] = _urllib_download) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    tarball = cache / TARBALL
    if not tarball.exists():
        part = cache / (TARBALL + ".part")
        try:
            download(URL, part)
        except Exception as exc:
            part.unlink(missing_ok=True)
            raise InteropSetupError(
                f"cannot download {URL} ({exc}); copy the tarball to {tarball} from a machine with network "
                "access (scp)"
            ) from exc
        try:
            verify_tarball(part)
        except InteropSetupError:
            part.unlink(missing_ok=True)
            raise
        part.replace(tarball)
    verify_tarball(tarball, advice="; delete the file and run again (or copy the right tarball there)")
    return tarball


def unpack(tarball: Path, cache: Path) -> Path:
    tree = cache / TREE
    if tree.exists():
        return tree
    staging = cache / (TREE + ".unpacking")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(tarball) as tar:
            for member in tar.getmembers():
                name = member.name
                if os.path.isabs(name) or ".." in Path(name).parts or Path(name).parts[0] != TOP_FOLDER:
                    raise InteropSetupError(f"{tarball} holds an unexpected path: {name}")
            tar.extractall(staging, filter="data")
        (staging / TOP_FOLDER).replace(tree)
    except (tarfile.TarError, OSError) as exc:  # a FilterError is a TarError
        raise InteropSetupError(f"cannot unpack {tarball}: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return tree


def tools_built(tree: Path) -> bool:
    return all((tree / "bin" / tool).is_file() and os.access(tree / "bin" / tool, os.X_OK) for tool in TOOLS)


def require_toolchain(*, platform: str | None = None, which=shutil.which) -> None:
    platform = sys.platform if platform is None else platform
    missing = []
    if not platform.startswith("linux"):
        missing.append(f"Linux (this is {platform})")
    missing += [tool for tool in ("gcc", "make", "stdbuf") if which(tool) is None]
    if missing:
        raise InteropSetupError("interop needs " + ", ".join(missing))


def ensure_built(tree: Path, *, rebuild: bool = False, run: Callable = subprocess.run) -> None:
    if tools_built(tree) and not rebuild:
        return
    # The committed bin/ of the tree must stay: the build writes the tools into it.
    result = run(["make", "clean", "all"], cwd=tree, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
        raise InteropSetupError(f"make clean all failed in {tree} (exit {result.returncode}):\n{tail}")
    if not tools_built(tree):
        raise InteropSetupError(f"make finished but {tree / 'bin'} lacks some of {', '.join(TOOLS)}")


def bin_dir(*, rebuild: bool = False) -> Path:
    require_toolchain()
    cache = cache_dir()
    tree = unpack(ensure_tarball(cache), cache)
    ensure_built(tree, rebuild=rebuild)
    return tree / "bin"
