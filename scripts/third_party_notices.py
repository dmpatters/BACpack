"""Regenerate THIRD-PARTY-NOTICES.txt: the licence of everything BACpack ships
that it didn't write, with each licence text.

Covers the pinned Python dependencies (packaging/constraints.txt, read from
this venv, which must have them installed at those pins), Python itself and
WinSW (the Windows bundle), and every npm package built into the web UI:
BACpack's own production dependencies (WEB_PACKAGES) plus the libraries
Swagger UI's prebuilt bundle carries inside it, found through that bundle's
source map and pinned by Swagger UI's own lockfile. Licence texts for npm
packages come from their registry tarballs. Needs internet access.

Rerun after a pin change in constraints.txt (tests/test_third_party_notices.py
fails until then), after an SPA rebuild that changes a WEB_PACKAGES version
(`npm ls --omit=dev --all` on the build machine), or a Swagger UI bump.
Usage: python scripts/third_party_notices.py"""
from __future__ import annotations

import io
import json
import re
import sys
import tarfile
import urllib.request
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "THIRD-PARTY-NOTICES.txt"
CONSTRAINTS = ROOT / "packaging" / "constraints.txt"

# The production dependency tree of the web UI build (loose-envify and
# js-tokens are left out: build-time transforms, not in the bundle).
WEB_PACKAGES = {
    "react": "18.3.1",
    "react-dom": "18.3.1",
    "scheduler": "0.23.2",
    "react-router": "6.30.6",
    "react-router-dom": "6.30.6",
    "@remix-run/router": "1.23.4",
    "@tanstack/react-query": "5.102.2",
    "@tanstack/query-core": "5.102.2",
    "swagger-ui-dist": "5.17.14",
}
SWAGGER_BUNDLE = "swagger-ui-bundle.js.map"
WINSW = ("WinSW", "2.12.0", "MIT", "https://raw.githubusercontent.com/winsw/winsw/v2.12.0/LICENSE.txt")

LICENCE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE)([.\-_].*)?$", re.IGNORECASE)
UA = {"User-Agent": "bacpack-third-party-notices"}


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as resp:
        return resp.read()


def _npm_tarball(name: str, version: str) -> tarfile.TarFile:
    base = name.split("/")[-1]
    data = _get(f"https://registry.npmjs.org/{name}/-/{base}-{version}.tgz")
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def _npm_licence(name: str, version: str, tar: tarfile.TarFile | None = None) -> tuple[str, list[str]]:
    tar = tar or _npm_tarball(name, version)
    declared, texts = "", []
    for member in sorted(tar.getmembers(), key=lambda m: m.name):
        parts = member.name.split("/")
        if not member.isfile() or len(parts) != 2:
            continue
        f = tar.extractfile(member)
        if f is None:
            continue
        if parts[1] == "package.json":
            pkg = json.loads(f.read().decode("utf-8"))
            lic = pkg.get("license") or pkg.get("licenses") or ""
            if isinstance(lic, dict):
                lic = lic.get("type", "")
            if isinstance(lic, list):
                lic = " OR ".join(x.get("type", "") if isinstance(x, dict) else str(x) for x in lic)
            declared = str(lic)
        elif LICENCE_FILE.match(parts[1]):
            texts.append(f.read().decode("utf-8", errors="replace").strip())
    return declared, texts


def _swagger_bundled(swagger_tar: tarfile.TarFile, version: str) -> dict[str, str]:
    """name -> version of every package inside Swagger UI's prebuilt bundle."""
    member = swagger_tar.extractfile(f"package/{SWAGGER_BUNDLE}")
    assert member is not None
    sources = json.loads(member.read().decode("utf-8"))["sources"]
    lock = json.loads(_get(f"https://raw.githubusercontent.com/swagger-api/swagger-ui/v{version}/package-lock.json"))
    packages = lock["packages"]
    found: dict[str, str] = {}
    for src in sources:
        m = re.search(r"(node_modules/(?:@[^/]+/)?[^/]+(?:/node_modules/(?:@[^/]+/)?[^/]+)*)", src)
        if not m:
            continue
        key = m.group(1)
        name = key.rsplit("node_modules/", 1)[1]
        entry = packages.get(key)
        if entry is None:
            # The published bundle can carry a package the tagged lockfile
            # lacks; npm's latest release stands in for its version.
            entry = json.loads(_get(f"https://registry.npmjs.org/{name}/latest"))
            print(f"{name}: not in Swagger UI's lockfile, using latest {entry['version']}", file=sys.stderr)
        found[f"{name}@{entry['version']}"] = entry["version"]
    return found


def _python_packages() -> list[tuple[str, str, str, list[str]]]:
    out = []
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, pin = line.split("==")
        dist = metadata.distribution(name)
        if dist.version != pin:
            sys.exit(f"{name} is {dist.version} in this venv, pinned {pin}: reinstall with -c packaging/constraints.txt")
        meta = dist.metadata
        lic = meta.get("License-Expression") or ""
        if not lic and meta.get("License") and len(meta["License"]) < 80:
            lic = meta["License"]
        if not lic:
            lic = "; ".join(c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::"))
        texts = []
        for f in dist.files or []:
            if ".dist-info" in str(f) and LICENCE_FILE.match(f.name):
                texts.append(Path(str(f.locate())).read_text(encoding="utf-8", errors="replace").strip())
        out.append((meta["Name"], dist.version, lic, texts))
    return out


def main() -> None:
    entries: list[tuple[str, str, str, list[str], str]] = []  # name, version, licence, texts, group
    for name, version, lic, texts in _python_packages():
        entries.append((name, version, lic, texts, "python"))
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    py_text = (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8", errors="replace").strip()
    entries.append(("Python", f"{py_ver} (Windows bundle)", "PSF-2.0", [py_text], "windows"))
    entries.append((WINSW[0], WINSW[1], WINSW[2], [_get(WINSW[3]).decode("utf-8").strip()], "windows"))

    swagger_tar = _npm_tarball("swagger-ui-dist", WEB_PACKAGES["swagger-ui-dist"])
    for name, version in WEB_PACKAGES.items():
        tar = swagger_tar if name == "swagger-ui-dist" else None
        lic, texts = _npm_licence(name, version, tar)
        entries.append((name, version, lic, texts, "web"))
    for key, version in sorted(_swagger_bundled(swagger_tar, WEB_PACKAGES["swagger-ui-dist"]).items()):
        name = key.rsplit("@", 1)[0]
        print(f"fetching {key}", file=sys.stderr)
        lic, texts = _npm_licence(name, version)
        entries.append((name, version, lic, texts, "swagger"))

    groups = {
        "python": "Python packages (installed by both installers; pinned in packaging/constraints.txt)",
        "windows": "Also in the Windows bundle",
        "web": "Built into the web UI (src/bacpack/web/spa)",
        "swagger": "Inside Swagger UI's prebuilt bundle (the web UI's API docs page)",
    }
    # Identical licence texts are printed once, listing every package they cover.
    by_text: dict[str, list[str]] = {}
    missing = []
    lines = [
        "BACpack third-party notices",
        "",
        "BACpack is MIT licensed (LICENSE). It ships or installs the software below,",
        "each under its own licence. Every licence text follows the list.",
        "Generated by scripts/third_party_notices.py.",
    ]
    for group, title in groups.items():
        lines += ["", "=" * 78, title, "=" * 78]
        for name, version, lic, texts, g in entries:
            if g != group:
                continue
            label = f"{name} {version}"
            lines.append(f"{label}: {lic or 'see licence text'}")
            if not texts:
                missing.append(label)
            for text in texts:
                by_text.setdefault(text, []).append(label)
    lines += ["", "=" * 78, "Licence texts", "=" * 78]
    for text, labels in by_text.items():
        lines += ["", "-" * 78, "Applies to: " + ", ".join(labels), "-" * 78, "", text]
    if missing:
        lines += ["", "=" * 78, "Packages that ship no licence file (their declared licence is listed above):", "=" * 78]
        lines += missing
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {TARGET} ({len(entries)} packages, {len(by_text)} distinct texts, {len(missing)} without a file)")


if __name__ == "__main__":
    main()
