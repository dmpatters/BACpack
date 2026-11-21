# Contributing to BACpack

[docs/architecture.md](docs/architecture.md) covers how BACpack is put together.

## Before you start

The [README](README.md) describes what BACpack does. [MAINTENANCE.md](MAINTENANCE.md) says what to expect from the project.

- Small fixes: open a pull request.
- Anything larger (a feature, object type, service, or a database or file format change): open an issue first.
- Bugs: open an issue with the bug report template. It asks for the diagnostics file (About page, Download diagnostics). Names, addresses and URLs are replaced unless you tick Use real values.
- Security problems: don't open an issue; follow [SECURITY.md](SECURITY.md).

## Set up

You need Python 3.13+ with SQLite 3.34+. Node.js is needed only for frontend work.

Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -c packaging/constraints.txt -e ".[dev]"
```

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c packaging/constraints.txt -e ".[dev]"
```

`packaging/constraints.txt` pins the versions the installers ship. Rerun the install after a pin changes.

Run the app from source (plain HTTP, development only; the services run `python -m bacpack serve`, HTTPS only).

```bash
.venv/bin/python -m uvicorn bacpack.web.app:app --host 0.0.0.0 --port 8000
```

```powershell
.\.venv\Scripts\python.exe -m uvicorn bacpack.web.app:app --host 0.0.0.0 --port 8000
```

Data goes in `./data/` (`bacpack.db`, `bacpack-history.db`); `BACPACK_DB_PATH` moves it. Give each device on one machine its own address or port.

The rest of this page shows Linux commands. On Windows use `.\.venv\Scripts\python.exe`, `ruff.exe` and `basedpyright.exe` from the same folder.

## Tests and checks

```bash
.venv/bin/python -m pytest                              # everything
.venv/bin/python -m pytest -n auto --dist loadgroup     # in parallel, as CI runs it
.venv/bin/python -m pytest tests/test_engine.py         # one file
.venv/bin/python -m pytest tests/test_engine.py::test_start_requires_bind_address   # one test
```

Run the suite on your own operating system before you open a pull request.

### Fixed UDP ports

A test that runs a device on a fixed UDP port, directly or through `FakeDeviceProcess` in `tests/fakes.py`, must be in a file listed in `UDP_TEST_FILES` in `tests/conftest.py`. A guard fails it anywhere else.

### Opt-in suites

These two suites are skipped unless asked for and need `-p no:xdist`.

`--scale` runs field-sized datasets and caches them under `~/.cache/bacpack-scale` (Windows: `%LOCALAPPDATA%\bacpack-scale`). The first run is slow and uses a lot of disk. Run it when a change affects how much work a device or the web process does.

```bash
.venv/bin/python -m pytest tests/scale --scale -p no:xdist
```

`--interop` runs the [bacnet-stack](https://github.com/bacnet-stack/bacnet-stack) 1.7.0 command-line tools against two BACpack devices on loopback. It is Linux only and needs `gcc`, `make` and `stdbuf`. The first run downloads the pinned source, checks its SHA-256 and builds into `~/.cache/bacpack-interop`. Run it when a change touches how a device answers on the wire.

```bash
.venv/bin/python -m pytest tests/interop --interop -p no:xdist
```

### Linter and type checker

Both must pass before a pull request.

```bash
.venv/bin/ruff check            # must print "All checks passed!"
.venv/bin/basedpyright          # must print "0 errors"
```

`ruff` has no baseline: fix a finding, or add `# noqa: RULE` and a short reason on its line.

`basedpyright` checks `src/bacpack` against `.basedpyright/baseline.json`. A new finding is a bug (fix it, test first) or a false alarm (`# pyright: ignore[ruleName]` on that line, with a reason). Never rewrite the baseline to hide one.

### Frontend

The web UI is a React app in `frontend/`. CI uses Node 22. There is no lockfile: use `npm install`, not `npm ci`.

```bash
cd frontend
npm install
npm run dev       # Vite dev server on :5173, proxies /api to uvicorn on :8000
npm run test      # vitest
npm run build     # tsc -b && vite build, into ../src/bacpack/web/spa
```

A change under `frontend/src` that ships must come with a rebuilt `src/bacpack/web/spa/`, committed after the last frontend change. `tests/test_spa_committed.py` checks this against the git history (skipped without a full clone). A change whose rebuild is byte-identical, such as a comment-only edit, adds a `SPA-unchanged: <reason>` line to the last paragraph of its commit message.

## Conventions a change follows

Most have a test that fails when broken.

- Tests first. A fix comes with a test that failed before it. A feature comes with tests for what it does and how it fails.
- bacpypes3 is reached only through `bacpypes3_compat.py`, which lists every overridden method and every internal depended on. `tests/test_bacpypes3_compat.py` fails on a new override or private access until it is registered there. A bacpypes3 upgrade starts there.
- Time goes through `timeutil.py` and `device_clock.py`. Stored timestamps are naive UTC. `tests/test_time_helpers_guard.py` refuses a raw `datetime.now`, `fromtimestamp` or `tzinfo=` elsewhere.
- What can be built is decided in `object_config.py` (`check_object`, `check_device`). A new rule goes there, not at a call site.
- BACpack imports sit at the top of a module and form no cycle (`tests/test_import_cycles_guard.py`). A name another module imports has no leading underscore (`tests/test_shared_names_guard.py`). Log lines name devices and objects only through `log_names.py` (`tests/test_log_names_guard.py`).
- Migrations are append-only. `migrations.py` keeps a numbered list per database file (`MIGRATIONS`, `HISTORY_MIGRATIONS`), tracked by `PRAGMA user_version`, plus `add_missing_columns` for a new nullable or defaulted column. Never edit an old entry.
- Schema changes are the maintainer's call, so open an issue first (a running device process reads the old columns, so never drop one). A change to the schema or to `CONFIG_VERSION` in `config_io.py` needs a frozen upgrade fixture set (`tests/test_upgrade_fixtures.py`).
- Saves that wait on a lock stay off the event loop. In a device process use `config_save.save`, `save_columns` or `run_in_order`. In the web process use a plain `def` route or `asyncio.to_thread`. A save that writes both database files calls `db.lock_both_files(session)` before its first change (`tests/_db_guard.py`).
- A new table a device reads needs a trigger in `migrations.py` (`TRIGGERS`) that bumps the change counter (`tests/test_change_triggers.py`).
- A new secret column uses `secrets_store`'s `EncryptedText` or `EncryptedHeaders` type, never encryption at a call site. A new file that holds a secret in the data folder goes through `data_files.hand_to_owner` and stays out of backups, exports and the diagnostics zip.
- `PICS.md` is generated. After changing a service, an object type, a property or a write rule, run `.venv/bin/python scripts/make_pics.py` and commit the result. `tests/test_pics.py` fails until it matches.
- Open an issue before changing a pin in `packaging/constraints.txt`. `THIRD-PARTY-NOTICES.txt` lists the licenses (`scripts/third_party_notices.py` regenerates it).
- Comments say why (a bacpypes3 quirk, a race, an ASHRAE 135 clause, a SQLite or cross-process constraint). Don't restate the code or put history in it (versions, dates, "was X before"). Cite a public issue or pull request as `dmpatters/BACpack#N`, never a bare `#N`.
- The upgrade promise. From 1.0.0 on, every 1.x release opens what an earlier one saved: the databases, `secret.key`, exports, backups and CSV point lists. See [docs/guide/upgrading.md](docs/guide/upgrading.md). A CSV column is never renamed or removed, and a rename keeps the old header as an alias. The write API (`/api/feed`, `/api/manual`, API-key sign-in) changes only by adding (`tests/web/test_write_api_shape.py`).
- Check a change to installers, service files, paths, permissions, firewall rules or upgrades on both Windows and Linux.
- Frontend. The UI follows `docs/design-system/`. A class added to `bundle.css` goes into both `frontend/src/design-system/bundle.css` and `docs/design-system/components/bundle.css`. `frontend/src/design-system/designSystem.test.ts` checks that `tokens.css` and `bundle.css` match the copies in `docs/design-system/` byte for byte.

## Versions and the changelog

A feature release bumps the middle number (`1.x.0`), a bug fix the last (`1.0.x`). Purpose decides, not size.

Don't edit `CHANGELOG.md` or the version number in a pull request. Put a one-line changelog entry in the description. The maintainer adds it to [CHANGELOG.md](CHANGELOG.md).

## Pull requests

A pull request is:

- One change. Unrelated fixes go in separate pull requests.
- Tested. New or changed behavior has tests, the full suite passes on your operating system (say which), and `ruff check` and `basedpyright` are clean.
- Described. Say what changed, why and how you checked it. If it touches the wire, name the BACnet client you tried.
- With a changelog line in the description.

### What happens to a pull request

The public repository holds one commit per release, built from the maintainer's private repository, so a pull request is not merged as it stands. Instead:

1. The maintainer applies your change in the development repository and keeps you as an author through a `Co-authored-by:` trailer. The trailer uses the name and email on your commit, so use your GitHub noreply address to keep your email private.
2. The pull request is closed with a note that names the release which ships the change.
3. The release's changelog entry credits your GitHub name.

A closed pull request with that note is not a refusal. If a change won't be taken, the note says why.

### AI-assisted contributions

These are welcome. BACpack itself was written almost entirely by an AI coding assistant (Anthropic's Claude, through Claude Code) under human direction. The bar is the same for every contribution: it includes tests, the suites pass, and you can explain the change and why it is right.

## License

BACpack is [MIT licensed](LICENSE). By submitting a contribution you agree that it is released under the same license. There is no contributor agreement to sign and no sign-off line to add.
