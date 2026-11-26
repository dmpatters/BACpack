"""Static checks on the service definitions; the
behaviour is checked live on the VMs."""
from __future__ import annotations

import configparser
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from bacpack import lifecycle

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def _unit():
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(PACKAGING / "bacpack.service", encoding="utf-8")
    return parser["Service"]


def test_unit_keeps_devices_alive_through_a_crash():
    assert _unit()["KillMode"] == "process"


def test_unit_gives_the_stop_time_to_stop_every_device():
    assert _unit()["TimeoutStopSec"] == "90"


def test_unit_runs_in_service_mode_and_does_not_retry_what_a_restart_cannot_fix():
    service = _unit()
    assert "BACPACK_SERVICE=1" in service["Environment"]
    assert service["RestartPreventExitStatus"].split() == [str(c) for c in lifecycle.NOT_RETRIED_EXIT_CODES]
    assert lifecycle.NOT_RETRIED_EXIT_CODES == (
        lifecycle.SQLITE_TOO_OLD_EXIT_CODE,
        lifecycle.WEB_CONFIG_BROKEN_EXIT_CODE,
        lifecycle.DATABASE_TOO_NEW_EXIT_CODE,
    )
    assert service["Restart"] == "on-failure"
    # A listen address that has not come up yet is worth retrying.
    assert str(lifecycle.ADDRESS_MISSING_EXIT_CODE) not in service["RestartPreventExitStatus"].split()


def test_unit_and_xml_run_the_serve_launcher_not_uvicorn():
    unit_text = (PACKAGING / "bacpack.service").read_text(encoding="utf-8")
    assert _template() and "uvicorn" not in unit_text and "uvicorn" not in _template()
    assert "0.0.0.0" not in unit_text and "0.0.0.0" not in _template()
    assert _unit()["ExecStart"].endswith("-P -m bacpack serve --port ${BACPACK_PORT}")
    assert _xml().findtext("arguments") == "-P -m bacpack serve --port 8000"


def test_shell_scripts_are_checked_out_with_lf():
    assert "*.sh text eol=lf" in (PACKAGING.parent / ".gitattributes").read_text(encoding="utf-8")


def _template():
    return (PACKAGING / "windows" / "bacpack-service.xml.template").read_text(encoding="utf-8")


def _xml(data=r"D:\ProgramData\BACpack"):
    text = _template().replace("{{PYTHON}}", "python.exe").replace("{{PORT}}", "8000")
    return ET.fromstring(text.replace("{{DATA}}", data))


def test_winsw_xml_runs_in_service_mode_with_a_90s_stop():
    root = _xml()
    env = {e.get("name"): e.get("value") for e in root.findall("env")}
    assert env["BACPACK_SERVICE"] == "1"
    assert root.findtext("stoptimeout") == "90 sec"
    # Restarts every non-zero exit; for what a restart cannot fix serve.main
    # stops its own service instead (test_serve.py).
    assert root.find("onfailure").get("action") == "restart"
    assert root.findtext("id") == "BACpack"


def test_winsw_xml_keeps_its_data_where_the_installer_locks_it_down():
    """ProgramData isn't always on C:; the installer fills {{DATA}}
    with the folder it creates and restricts."""
    assert "ProgramData" not in _template()
    root = _xml()
    env = {e.get("name"): e.get("value") for e in root.findall("env")}
    assert env["BACPACK_DB_PATH"] == r"D:\ProgramData\BACpack\bacpack.db"
    assert root.findtext("workingdirectory") == r"D:\ProgramData\BACpack"
    assert root.findtext("logpath") == r"D:\ProgramData\BACpack\logs"
    install = (PACKAGING / "install.ps1").read_text(encoding="utf-8")
    assert ".Replace('{{DATA}}', $DataDir)" in install


def test_release_workflow_writes_only_from_the_tag_only_job():
    """The build runs a PR's own build_bundle.ps1, so only the job gated on
    a tag may hold contents: write."""
    text = (PACKAGING.parent / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    top, _, jobs = text.partition("\njobs:\n")
    assert "permissions:\n  contents: read" in top
    # Any spelling of a write grant counts: a key, write-all, or a flow mapping.
    grants = re.compile(r"permissions:\s*(write-all|\{[^}]*write|(\n[ ]+[\w-]+:\s*\w+)*\n[ ]+[\w-]+:\s*write)")
    assert not grants.search(top)
    writers = [job for job in jobs.split("\n\n") if grants.search(job)]
    assert len(writers) == 1
    assert "if: startsWith(github.ref, 'refs/tags/v')" in writers[0]
    # The writer must not run anything from the PR's packaging folder, directly or through build_installer.ps1.
    for name in ("build_bundle.ps1", "build_installer.ps1", "packaging"):
        assert name not in writers[0]


# --- login flags; behaviour is checked live on the VMs -----------------------------


def _install_sh():
    return (PACKAGING / "install.sh").read_text(encoding="utf-8")


def _install_ps1():
    return (PACKAGING / "install.ps1").read_text(encoding="utf-8")


def test_pyproject_license_is_an_spdx_string():
    """The table form makes setuptools print a deprecation block at every source
    install and is dropped after 2027-02-18; the string form needs setuptools 77."""
    import tomllib

    data = tomllib.loads((PACKAGING.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["license"] == "MIT"
    assert "setuptools>=77" in data["build-system"]["requires"]


def test_build_bundle_installs_with_a_python_of_the_bundles_version():
    """pip installs wheels for the Python it runs under: a 3.13 first on PATH
    puts cp313 wheels into the 3.14 bundle, which then fails its smoke test."""
    script = (PACKAGING / "windows" / "build_bundle.ps1").read_text(encoding="utf-8")
    assert "& python " not in script
    assert "& $BuildPy -m pip install --target" in script
    check = script.index("$BuildPy -c")
    assert check < script.index("& $BuildPy -m pip install")
    assert "py.exe" in script or "& py " in script
    assert "-Python" in script and "sys.maxsize" in script


def test_the_windows_bundle_ships_security_md():
    """The install summary tells the reader to read SECURITY.md, so the bundle carries it."""
    script = (PACKAGING / "windows" / "build_bundle.ps1").read_text(encoding="utf-8")
    assert any("Copy-Item" in line and "SECURITY.md" in line for line in script.splitlines())


def test_install_sh_has_the_login_flags_and_no_password_argument():
    text = _install_sh()
    assert "--login-user)" in text and "--no-login)" in text
    assert "BACPACK_LOGIN_PASSWORD" in text
    assert "--login-password" not in text and "--password" not in text


def test_install_ps1_has_the_login_parameters():
    text = _install_ps1()
    param_block = text[text.index("param("):text.index("$ErrorActionPreference")]
    assert "$LoginUser" in param_block and "$NoLogin" in param_block
    assert "LoginPassword" not in param_block and "$Password" not in param_block


def test_both_installers_reject_the_two_flags_together():
    assert "--login-user and --no-login can't be used together" in _install_sh()
    assert "-LoginUser and -NoLogin can't be used together" in _install_ps1()


def test_both_installers_apply_the_login_before_starting_the_service():
    sh = _install_sh()
    start = sh.index('if [[ $START -eq 1 ]]; then\n  echo "Starting BACpack..."')
    assert sh.index("set-login --username") < start
    assert sh.index('"$ADMIN_BIN" no-login') < start
    ps1 = _install_ps1()
    start = ps1.index("if ($Start) {\n    Write-Host 'Starting BACpack...'\n    Start-Service")
    assert ps1.index("set-login --username") < start
    assert ps1.index("& $AdminCmd no-login") < start


def test_the_password_never_goes_on_a_command_line():
    sh = _install_sh()
    assert 'set-login --username "$LOGIN_USER"' in sh and "$LOGIN_PASSWORD\"" not in sh.split("set-login")[1].split("\n")[0]
    assert "set-login --username $LoginUser" in _install_ps1()


def test_both_installers_write_the_wrapper_and_uninstall_removes_it():
    sh = _install_sh()
    # /usr/sbin, not /usr/local/bin: RHEL-family sudo's secure_path
    # (/sbin:/bin:/usr/sbin:/usr/bin) leaves /usr/local/bin out, so
    # "sudo bacpack-admin" would be "command not found" there.
    assert "ADMIN_BIN=/usr/sbin/bacpack-admin" in sh
    assert 'rm -f "$UNIT" "$ADMIN_BIN"' in sh
    assert "-m bacpack.admin" in sh and "runuser -u $SERVICE_USER -m" in sh
    ps1 = _install_ps1()
    assert "bacpack-admin.cmd" in ps1 and "-m bacpack.admin %*" in ps1
    # Uninstall removes the whole install folder, which holds the .cmd.
    assert "Remove-Item $InstallDir -Recurse -Force" in ps1


def test_closing_message_no_longer_says_there_is_no_login_yet():
    for text in (_install_sh(), _install_ps1()):
        assert "has no login yet" not in text
        assert "create a login" in text and "first visit" in text and "claims it" in text


def test_the_admin_wrappers_keep_the_current_directory_off_sys_path():
    assert "python -P -m bacpack.admin" in _install_sh()
    assert "-P -m bacpack.admin %*" in _install_ps1()


def test_the_service_ignores_forwarded_for_headers():
    """The services run the launcher, which starts uvicorn with proxy_headers
    off (tests/test_serve.py checks the call)."""
    assert "bacpack serve" in _unit()["ExecStart"] and "bacpack serve" in _template()
    assert "proxy_headers=False" in (PACKAGING.parent / "src" / "bacpack" / "serve.py").read_text(encoding="utf-8")


def test_install_sh_enables_the_unit_only_after_the_login_step_worked():
    sh = _install_sh()
    assert sh.index("systemctl enable bacpack.service") > sh.index('"$ADMIN_BIN" no-login')


def test_install_ps1_unregisters_a_fresh_service_when_the_login_step_fails():
    """A fresh install that failed here would otherwise start on the next boot
    in setup mode, open to the network. An upgrade's service stays: its
    database already holds its login choice."""
    ps1 = _install_ps1()
    block = ps1[ps1.index("Write-Host 'Creating the login...'"):ps1.index("# --- firewall")]
    failures = [l for l in block.splitlines() if "$LASTEXITCODE" in l and "Fail" in l]
    assert len(failures) == 3  # exit 2, any other non-zero, and the no-login step
    for line in failures:
        assert "if ($Fresh) { Remove-Service-Registration }" in line, line
        assert line.index("Remove-Service-Registration") < line.index("Fail"), line


def _sh_start_block(sh: str) -> str:
    at = sh.index('if [[ $START -eq 1 ]]; then\n  echo "Starting BACpack..."')
    return sh[at:sh.index("is installed.", at)]


def test_install_sh_checks_the_service_still_runs_after_starting_it():
    block = _sh_start_block(_install_sh())
    # Until the web port listens, not a fixed sleep: a start on a taken port
    # dies after the first few seconds and systemd restarts it every 5 s.
    assert "sleep 5" not in block
    loop = block[block.index("for _ in $(seq 90)"):]
    assert "systemctl is-active --quiet bacpack.service" in loop
    assert "systemctl show -p MainPID --value bacpack.service" in block
    assert 'port_owner "$PORT"' in loop
    assert "journalctl -u bacpack -n 20 --no-pager" in loop
    assert "systemctl stop bacpack.service" in loop[:loop.index("exit 1")]
    assert "warning: BACpack is running but" in block


def test_install_sh_refuses_a_taken_web_port_before_changing_anything():
    sh = _install_sh()
    assert "port_owner() {" in sh and "ss -Hltnp" in sh
    check = sh.index('owner=$(port_owner "$PORT")')
    # After the old service stopped (it held the port), before any change.
    assert sh.index("systemctl stop bacpack.service") < check < sh.index("# --- user, data directory")
    assert "[[ $START -eq 1 ]]" in sh[check - 200:check]
    assert "is in use by" in sh[check:check + 400]


def test_install_sh_does_not_restart_the_previous_install_after_a_failed_start():
    sh = _install_sh()
    # The exit trap starts the old install only while RESTORE_ON_FAIL is 1, and
    # that is cleared once the new code is in place (before login and start).
    assert "$RESTORE_ON_FAIL -eq 1" in sh.split("on_exit() {")[1].split("trap on_exit")[0]
    assert sh.index("RESTORE_ON_FAIL=0\n") < sh.index("set-login --username") < sh.index('echo "Starting BACpack..."')


def test_install_sh_left_stopped_message_cannot_follow_a_start():
    sh = _install_sh()
    # WAS_ACTIVE says whether the service ran before the upgrade; the final
    # "left stopped" line reads it, so nothing may reset it after the start.
    assert sh.count("WAS_ACTIVE=0") == 1
    assert sh.index("WAS_ACTIVE=0") < sh.index("WAS_ACTIVE=1")


def test_install_ps1_checks_the_service_still_runs_after_starting_it():
    ps1 = _install_ps1()
    start = ps1.index("Start-Service $ServiceName\n    # WinSW")
    assert "ProcessId" in ps1[start:] and "Start-Sleep" in ps1[start:]
    assert "-Tail 20" in ps1[start:] and "-Filter '*.log'" in ps1[start:]
    assert "Sort-Object LastWriteTime -Descending | Select-Object -First 3" in ps1[start:]
    assert "Stop-Service $ServiceName" in ps1[start:ps1.index("exit 1", start)]
    assert ps1.index("exit 1", start) < ps1.index("is installed (")
    # Until the web port listens, not a fixed 6 s: a start on a taken port dies
    # after that and WinSW restarts it every 5 s.
    block = ps1[start:ps1.index("is installed (")]
    assert "-lt 90" in block and "Get-PortOwner $Port" in block
    assert "warning: BACpack is running but" in block


def test_install_ps1_refuses_a_taken_web_port_before_changing_anything():
    ps1 = _install_ps1()
    assert "function Get-PortOwner" in ps1 and "Get-NetTCPConnection -State Listen" in ps1
    check = ps1.index("$owner = Get-PortOwner $Port")
    # After the old service stopped (it held the port), before any change.
    assert ps1.index("$existing.WaitForStatus('Stopped'") < check < ps1.index("# --- data directory")
    assert "if ($Start)" in ps1[check - 100:check]
    assert "is in use by" in ps1[check:check + 300]


def test_both_installers_show_bacpack_admins_own_refusal():
    assert "(( $? == 2 ))" in _install_sh()
    assert "$LASTEXITCODE -eq 2" in _install_ps1()


# --- HTTPS; behaviour is checked live on the VMs -----------------------------


def test_both_installers_print_https_urls():
    for text in (_install_sh(), _install_ps1()):
        web_ui = [line for line in text.splitlines() if "Web UI:" in line]
        assert len(web_ui) == 1 and "https://" in web_ui[0]
        assert "http://" not in text


def test_both_installers_tell_where_the_browser_warning_comes_from():
    for text in (_install_sh(), _install_ps1()):
        assert "self-signed" in text and "warns" in text
        assert "The Web Server page shows its fingerprint." in text
        assert "Settings > Web server" not in text


def test_admin_wrappers_export_the_port():
    assert "export BACPACK_PORT=$PORT" in _install_sh()
    assert "set BACPACK_PORT=$Port" in _install_ps1()


def test_bundle_smoke_makes_and_loads_a_certificate():
    text = (PACKAGING / "windows" / "bundle_smoke.py").read_text(encoding="utf-8")
    assert "tls_certs" in text and "load_cert_chain" in text


def test_install_docs_say_how_to_use_port_443():
    readme = (PACKAGING.parent / "README.md").read_text(encoding="utf-8")
    for needle in ("--port 443", "-Port 443", "bacpack.service.d", "override.conf", "AmbientCapabilities=CAP_NET_BIND_SERVICE"):
        assert needle in readme
    # The capability first: the installer refuses a port below 1024 without it.
    assert readme.index("bacpack.service.d") < readme.index("install.sh --port 443")
    # systemctl edit needs the unit to exist, which a fresh machine lacks.
    assert "systemctl edit" not in readme


def test_both_installers_point_at_security_md():
    assert "SECURITY.md" in _install_sh()
    assert "SECURITY.md" in _install_ps1()


def test_install_ps1_lets_the_service_account_stop_its_own_service():
    """serve.main stops its own service for what a restart cannot fix; the
    account needs SERVICE_STOP (WP) and SERVICE_QUERY_STATUS (LC) on it."""
    text = (PACKAGING / "install.ps1").read_text(encoding="utf-8")
    assert '$ace = "(A;;LCWP;;;$sid)"' in text
    assert "sc.exe sdset $ServiceName $sddl" in text
    # After the account is set: the SID of NT SERVICE\BACpack resolves once the service exists.
    assert text.index("sc.exe config $ServiceName obj=") < text.index("sc.exe sdset $ServiceName")


# --- a re-run starts the service until a start has held ----------


def test_install_sh_records_a_held_start_and_only_then():
    sh = _install_sh()
    block = sh[sh.index('if [[ $START -eq 1 ]]; then\n  echo "Starting BACpack..."'):]
    assert block.index("BACPACK_STARTED=1") > block.index("is-active --quiet bacpack.service")
    assert block.index("exit 1") < block.index("BACPACK_STARTED=1")
    assert "STARTED_BEFORE=" in sh and "env_value BACPACK_STARTED" in sh


def test_install_sh_starts_unless_the_user_stopped_a_marked_install():
    sh = _install_sh()
    assert "$STARTED_BEFORE -eq 0 || $WAS_ACTIVE -eq 1" in sh
    at = sh.index("printf 'BACPACK_PORT")
    rewrite = sh[at:sh.index("systemctl daemon-reload", at)]
    assert "$START -eq 0" in rewrite and "BACPACK_STARTED=1" in rewrite
    assert "FRESH" not in sh


def test_install_sh_hint_and_left_stopped_follow_the_marker():
    sh = _install_sh()
    assert "elif [[ $STARTED_BEFORE -eq 0 && $WAS_ACTIVE -eq 0 ]]" in sh
    assert 'if [[ $START -eq 0 ]]; then\n  echo\n  echo "The service was stopped before the upgrade' in sh


def test_install_sh_failed_login_messages_say_to_run_the_installer_again():
    sh = _install_sh()
    lines = [line for line in sh.splitlines() if "was not started" in line and "die" in line]
    assert len(lines) == 3
    for line in lines:
        assert "run this installer again" in line.lower()
    assert "Run: sudo bacpack-admin set-login" not in sh


def test_install_ps1_records_a_held_start_and_only_then():
    ps1 = _install_ps1()
    block = ps1[ps1.index("if ($Start) {\n    Write-Host 'Starting BACpack...'\n    Start-Service"):]
    assert block.index("Set-Content $StartedMarker") > block.index("if (-not $held)")
    assert block.index("exit 1") < block.index("Set-Content $StartedMarker")
    assert "$StartedMarker = Join-Path $InstallDir 'started.marker'" in ps1


def test_install_ps1_drops_the_marker_when_it_will_start_and_keeps_it_when_not():
    ps1 = _install_ps1()
    assert "$Start = -not $HadMarker -or $WasRunning" in ps1
    drop = ps1.index("if ($Start) { Remove-Item $StartedMarker")
    assert ps1.index("$RestoreOnFail = $false") < drop < ps1.index("set-login --username")
    assert "elseif (-not $HadMarker -and -not $WasRunning -and $LoginNotChosen)" in ps1
    assert "if (-not $Start) {\n    Write-Host \"\"\n    Write-Host 'The service was stopped before the upgrade" in ps1
    assert ps1.count("Remove-Item $StartedMarker") == 1


# --- icacls names accounts by SID ----------


def test_install_ps1_names_accounts_by_sid_in_every_icacls():
    lines = [line for line in _install_ps1().splitlines() if line.lstrip().startswith("icacls")]
    assert len(lines) == 2
    data = next(line for line in lines if "$DataDir" in line)
    assert "*S-1-5-18:(OI)(CI)F" in data and "*S-1-5-32-544:(OI)(CI)F" in data
    assert "*${sid}:(OI)(CI)M" in data
    for line in lines:
        assert "SYSTEM:" not in line and "Administrators:" not in line and "${Account}:" not in line


def test_install_ps1_checks_each_icacls_result():
    lines = _install_ps1().splitlines()
    for target in ("$InstallDir", "$DataDir"):
        at = next(i for i, line in enumerate(lines) if line.startswith("icacls") and target in line)
        assert "$LASTEXITCODE" in lines[at + 1]


# --- the data dir follows a recreated service user ----------


def test_install_sh_reowns_the_whole_data_dir_before_restorecon():
    sh = _install_sh()
    # The trailing slash: a symlinked data dir is followed once, and links inside stay links (-h).
    chown = 'chown -hR "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/"'
    assert chown in sh
    assert sh.index(chown) < sh.index('restorecon -R "$INSTALL_DIR" "$DATA_DIR"')
    assert "-exec chown" not in sh
    assert "chmod 640" in sh


def test_install_sh_drops_the_marker_at_the_point_of_no_return():
    """Between the point of no return and the env file's rewrite, restorecon,
    the unit install and the firewall can each abort the script."""
    sh = _install_sh()
    point = sh.rindex("\nRESTORE_ON_FAIL=0\n")
    drop = sh.index("'/^BACPACK_STARTED=/d' \"$ENV_FILE\"")
    assert point < drop < sh.index("restorecon -R")
    assert "if [[ $START -eq 1 && -f $ENV_FILE ]]; then" in sh[drop - 120:drop]


# --- installer output: the address and the summary's width ------------------

_SUMMARY_VALUES = {
    "version": "0.46.10", "mode": "bundle", "Port": "8000", "PORT": "8000", "BacnetPort": "47808",
    "BACNET_PORT": "47808", "reach": "192.168.1.50", "REACH": "192.168.1.50", "ip": "192.168.1.50",
    "DataDir": r"C:\ProgramData\BACpack", "AdminCmd": r"C:\Program Files\BACpack\bacpack-admin.cmd",
    "LoginUser": "operator", "LOGIN_USER": "operator", "FirewallGroup": "BACpack", "KIND": "firewalld",
    "PyPrefix": r"C:\Python314", "PY_PREFIX": "/root/.local/share/uv/python/cpython-3.14.8-linux-x86_64-gnu", "PY_OWNER": "alice",
    "writers": r"BUILTIN\Users, NT AUTHORITY\Authenticated Users", "VERSION": "0.46.10", "NoLoginWarning": "x",
}


def _summary_lines(text: str) -> list[str]:
    """The printed text of each output line from 'is installed' on, with variables
    replaced by representative values."""
    import re

    at = text.rindex("is installed")
    region = text[text.rindex(chr(10), 0, at):]
    lines = []
    for raw in region.splitlines():
        match = re.search(r"""(?:echo|Write-Host)\s+(?:"((?:`.|[^"`])*)"|'((?:''|[^'])*)')""", raw)
        if not match:
            continue
        if match.group(1) is not None:
            body = re.sub(r"`(.)", r"\1", match.group(1))
        else:
            body = match.group(2).replace("''", "'")
        body = re.sub(r"\$\((?:[^()]|\([^()]*\))*\)", "x", body)
        body = re.sub(r"\$\{?(\w+)\}?", lambda m: _SUMMARY_VALUES[m.group(1)], body)
        lines.append(body)
    return lines


def test_both_installers_keep_every_summary_line_under_80_columns():
    for text in (_install_sh(), _install_ps1()):
        lines = _summary_lines(text)
        assert len([line for line in lines if line]) >= 18
        # A command on its own line may carry a long path.
        too_long = [line for line in lines if len(line) >= 80 and not line.lstrip().startswith(("sudo ", '"'))]
        assert not too_long


def test_install_ps1_picks_the_address_on_the_default_route_with_a_fallback():
    ps1 = _install_ps1()
    fn = ps1[ps1.index("function Get-LanAddress"):]
    fn = fn[:fn.index("\n}\n")]
    assert "0.0.0.0/0" in fn and "RouteMetric" in fn and "InterfaceMetric" in fn
    assert "-PrefixOrigin Dhcp, Manual" in fn and "169.254" in fn
    # The route lookup and the fallback each have their own try/catch.
    assert fn.count("} catch") == 2 and "'Preferred'" in fn and "Select-Object -First 1" in fn.split("foreach")[0]
    assert "$ip = Get-LanAddress" in ps1


def test_install_sh_picks_the_address_on_the_default_route_with_a_fallback():
    sh = _install_sh()
    assert "ip -4 route get 192.0.2.1" in sh and "hostname -I" in sh
    assert sh.index("ip -4 route get") < sh.index("hostname -I")
    for line in sh.splitlines():
        if "ip -4 route get" in line or "hostname -I" in line:
            assert "|| true" in line


def test_both_installers_print_localhost_and_the_other_computer_address():
    for text in (_install_sh(), _install_ps1()):
        assert "https://localhost:" in text and "from another computer: https://" in text
        assert "this computer's address" in text


def test_both_installers_print_a_line_per_stage():
    ps1, sh = _install_ps1(), _install_sh()
    for line in ("Registering the service...", "Creating the login...", "Opening the firewall...",
                 "Starting BACpack..."):
        assert line in ps1 and line in sh
    assert "Copying BACpack's files..." in ps1
    # The scripts do the steps in different orders (Linux opens the firewall before the login).
    order = {
        "ps1": (ps1, ("Copying BACpack's", "Registering the service", "Creating the login",
                      "Opening the firewall", "Starting BACpack")),
        "sh": (sh, ("Registering the service", "Opening the firewall", "Creating the login",
                    "Starting BACpack")),
    }
    for text, steps in order.values():
        positions = [text.index(step) for step in steps]
        assert positions == sorted(positions)


# --- uninstall from the installed copy, and the Windows setup -----------------------


def test_service_description_does_not_repeat_the_name():
    assert "Description=BACnet server (device simulator + gateway)" in (PACKAGING / "bacpack.service").read_text(encoding="utf-8")
    assert _xml().findtext("description") == "BACnet server (device simulator + gateway)"
    # Both installers write the unit / XML again at every run, so an upgrade carries a change.
    assert 'install -m 644 "$STAGE/packaging/bacpack.service" "$UNIT"' in _install_sh()
    assert "Set-Content $WinswXml" in _install_ps1()


def test_install_sh_keeps_a_copy_of_itself_for_uninstall():
    sh = _install_sh()
    assert '"$INSTALL_DIR/install.sh"' in sh
    # Before the chown to root, so the kept copy is root-owned.
    assert sh.index('"$INSTALL_DIR/install.sh"') < sh.index('chown -R root:root "$INSTALL_DIR"')


def test_linux_wrapper_uninstalls_from_a_temp_copy_not_from_python():
    sh = _install_sh()
    wrapper = sh[sh.index("cat > \"$ADMIN_BIN\""):sh.index("chmod 755 \"$ADMIN_BIN\"")]
    root_check = wrapper.index("run as root")
    branch = wrapper.index("uninstall")
    assert root_check < branch < wrapper.index("-m bacpack.admin")
    assert "mktemp" in wrapper
    # --purge travels through "$@"; the copy is removed and its status returned.
    assert r'--uninstall "\$@"' in wrapper
    assert r'rm -f "\$tmp"' in wrapper and r"exit \$status" in wrapper
    # Python is not involved: the venv is deleted by the uninstall.
    uninstall_block = wrapper[branch:wrapper.index("export BACPACK_DB_PATH")]
    assert "bacpack.admin" not in uninstall_block


def test_install_ps1_keeps_a_copy_of_itself_for_uninstall():
    ps1 = _install_ps1()
    assert "Copy-Item $PSCommandPath $KeptScript -Force" in ps1 and "Join-Path $InstallDir 'install.ps1'" in ps1
    # The copy is made after the folder exists and before the service is registered.
    assert ps1.index("Join-Path $InstallDir 'install.ps1'") < ps1.index("Registering the service...")


def _windows_wrapper_lines():
    ps1 = _install_ps1()
    start = ps1.index("$AdminCmd = Join-Path $InstallDir 'bacpack-admin.cmd'")
    return ps1[start:ps1.index("Set-Content $AdminCmd")]


def test_windows_wrapper_hands_uninstall_to_a_temp_copy_of_itself():
    """cmd re-reads a batch file after each command, even after `exit /b` inside
    a block, so the uninstall (which deletes the wrapper) must run from a copy:
    run from the installed one, it printed "The system cannot find the path
    specified" and exited 1 after a successful uninstall."""
    text = _windows_wrapper_lines()
    # The copy is recognized by its own name, not by an environment variable that
    # would stay set in the caller's cmd window.
    handoff = text[text.index('if /i "%~1"=="uninstall" if /i not "%~nx0"=="bacpack-uninstall.cmd" ('):]
    handoff = handoff[:handoff.index("')',")]
    assert 'copy /y "%~f0" "%TEMP%\\bacpack-uninstall.cmd"' in handoff
    assert 'copy /y "%~dp0install.ps1" "%TEMP%\\bacpack-uninstall.ps1"' in handoff
    # Started without CALL, so control never comes back to the installed file.
    last = handoff.strip().splitlines()[-1].strip().strip("',").strip()
    assert last == '"%TEMP%\\bacpack-uninstall.cmd" %*'
    assert "call " not in handoff.lower()
    # The copy runs the uninstall from its own folder.
    run = text[text.index('if /i "%~1"=="uninstall" (', text.index("bacpack-uninstall.cmd")):]
    run = run[:run.index("exit /b !BP_RC!")]
    assert '-File "%~dp0bacpack-uninstall.ps1" -Uninstall !BP_PURGE!' in run
    assert "--purge" in run and "-purge" in run
    assert text.index("exit /b !BP_RC!") < text.index("bacpack.admin")


def _iss():
    return (PACKAGING / "windows" / "bacpack.iss").read_text(encoding="utf-8")


def test_setup_wizard_is_an_elevated_64_bit_setup_with_a_fixed_app_id():
    iss = _iss()
    assert "AppId={{8728BC0C-AF8A-4BA1-9FCE-1CA761573BA5}" in iss
    assert "{8728BC0C-AF8A-4BA1-9FCE-1CA761573BA5}_is1" in iss  # the uninstall key the downgrade check reads
    assert "PrivilegesRequired=admin" in iss
    assert "ArchitecturesInstallIn64BitMode=x64compatible" in iss
    assert "DisableDirPage=yes" in iss
    assert r"DefaultDirName={commonpf}\BACpack Setup" in iss
    assert "OutputBaseFilename=BACpack-{#AppVersion}-setup" in iss
    assert "AppName=BACpack" in iss


def test_setup_wizard_carries_the_bundle_in_its_temp_folder_not_in_program_files():
    iss = _iss()
    assert r'DestDir: "{tmp}\bacpack-bundle"' in iss
    assert "{#BundleDir}" in iss
    # Carried, not copied by Inno: PrepareToInstall extracts it on demand.
    assert "dontcopy" in iss and 'DestDir: "{app}"' not in iss


def test_setup_wizard_start_menu_and_finish_page():
    iss = _iss()
    assert r'Name: "{group}\Uninstall BACpack"; Filename: "{uninstallexe}"' in iss
    assert "BACpack web UI.url" in iss and "https://localhost:{code:GetPort}" in iss
    assert 'Description: "Open the BACpack web UI"' in iss and "postinstall" in iss and "skipifsilent" in iss


def test_setup_wizard_has_the_port_and_login_pages_and_the_silent_port():
    iss = _iss()
    assert "{param:PORT|}" in iss
    assert "'--port '" in iss  # the installed port, read from the service XML
    assert "Choose on the first visit to the web UI" in iss and "Create a login now" in iss
    assert "Length(Password) < 12" in iss
    # An upgrade keeps its login: the login pages are skipped.
    assert "Result := HadInstall or HadData\n  else if PageID = CredPage.ID" in iss
    assert "Result := HadInstall or HadData or (not LoginChoicePage.Values[1]);" in iss


def test_setup_wizard_refuses_a_downgrade():
    iss = _iss()
    assert "CompareVersions(Installed, '{#AppVersion}') > 0" in iss
    assert "SuppressibleMsgBox" in iss


def test_setup_wizard_never_puts_the_password_on_a_command_line():
    iss = _iss()
    assert "BACPACK_LOGIN_PASSWORD" in iss
    for line in iss.splitlines():
        # The password's only uses: the environment variable and the checker.
        if "CredPage.Values[1]" in line:
            assert "SetEnvironmentVariableW('BACPACK_LOGIN_PASSWORD'" in line or "CheckLogin(" in line, line
        # Any variable the command line is built in.
        if re.match(r"\s*(Arguments|Params|Options|Parameters)\s*:=", line) or line.lstrip().startswith("'"):
            assert "Values[1]" not in line and "Password" not in line, line
    # Set in this process around the run and cleared in a finally, even when the run throws.
    prepare = iss[iss.index("function PrepareToInstall"):iss.index("procedure CurUninstallStepChanged")]
    assert prepare.index("SetEnvironmentVariableW('BACPACK_LOGIN_PASSWORD', CredPage.Values[1])") < prepare.index("RunInstallScript(Dir, Arguments)")
    assert prepare.index("finally") < prepare.index("SetEnvironmentVariableW('BACPACK_LOGIN_PASSWORD', '')")


def test_setup_wizard_reports_a_failure_with_the_output_and_undoes_a_fresh_install():
    iss = _iss()
    prepare = iss[iss.index("function PrepareToInstall"):iss.index("procedure CurUninstallStepChanged")]
    assert "TailLines(OutputLines, 15)" in prepare
    # A failed fresh install is removed again, data kept; an upgrade is left to install.ps1's own rollback.
    assert "RunInstallScript(Dir, '-Uninstall')" in prepare
    assert "-Purge" not in prepare
    assert "if HadInstall then" in prepare


def test_setup_wizard_uninstall_asks_about_the_data_with_no_as_the_default():
    iss = _iss()
    uninstall = iss[iss.index("procedure CurUninstallStepChanged"):]
    assert "MB_YESNO or MB_DEFBUTTON2" in uninstall
    assert "if not UninstallSilent then" in uninstall
    assert "' -Purge'" in uninstall and uninstall.index("if Purge then") > uninstall.index("UninstallSilent")
    # The script deletes its own folder, so a temp copy of the kept one runs.
    assert r"install.ps1" in uninstall and "CopyFile(Kept" in uninstall and "-Uninstall" in uninstall


def _build_installer():
    return (PACKAGING / "windows" / "build_installer.ps1").read_text(encoding="utf-8")


def test_build_installer_pins_the_compiler_and_checks_it_before_running_it():
    script = _build_installer()
    assert "$InnoVersion = '6.7.4'" in script
    assert "A549929319F9120716677C685535CA4671E4B937B5FDC87E2585F32D8EB30E8C" in script
    assert "is-6_7_4/innosetup-6.7.4.exe" in script
    assert script.index("Get-FileHash $download") < script.index("Start-Process $download")


def test_build_installer_installs_the_compiler_for_the_current_user_only():
    script = _build_installer()
    assert "/CURRENTUSER" in script and "$env:LOCALAPPDATA" in script
    assert "ProgramFiles" not in script
    assert "-Clean" in script or "[switch]$Clean" in script


def test_build_installer_builds_the_bundle_first_and_hands_the_version_and_folder_to_iscc():
    script = _build_installer()
    # The first mentions are in the header comment: compare the lines that run them.
    lines = script.splitlines()
    bundle = next(i for i, l in enumerate(lines) if l.lstrip().startswith("&") and "build_bundle.ps1" in l)
    iscc = next(i for i, l in enumerate(lines) if l.lstrip().startswith("& $iscc") and "bacpack.iss" in l)
    assert bundle < iscc
    assert "/DAppVersion=" in script and "/DBundleDir=" in script


def test_release_workflow_builds_and_attaches_the_setup_exe_next_to_the_zip():
    text = (PACKAGING.parent / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "build_installer.ps1" in text
    assert "dist/BACpack-*-setup.exe" in text
    assert text.count("BACpack-*-setup.exe") >= 2  # uploaded as an artifact and attached to the release


# --- processes are named BACpack ------------------------------------------------------


def _build_bundle():
    return (PACKAGING / "windows" / "build_bundle.ps1").read_text(encoding="utf-8")


def test_the_bundle_carries_bacpack_exe_and_bacpack_device_exe_with_their_own_descriptions():
    script = _build_bundle()
    assert "rcedit-x64.exe" in script and "3E7801DB1A5EDBEC91B49A24A094AAD776CB4515488EA5A4CA2289C400EADE2A" in script
    assert script.index("Get-Checked $RceditUrl $RceditSha256") < script.index("& $rcedit")
    assert "@('bacpack.exe', 'BACpack')" in script and "@('bacpack-worker.exe', 'BACpack device')" in script
    assert "--set-version-string FileDescription" in script and "--set-version-string ProductName 'BACpack'" in script
    # Taken from the embeddable python.exe, which stays in the bundle; rcedit itself is not shipped.
    assert r'Copy-Item "$out\python\python.exe" $copy' in script
    assert "Remove-Item $rcedit" in script
    # The smoke test runs as the renamed interpreter and checks the device child's executable.
    assert r'& "$out\python\bacpack.exe" "$root\packaging\windows\bundle_smoke.py"' in script
    smoke = (PACKAGING / "windows" / "bundle_smoke.py").read_text(encoding="utf-8")
    assert "bacpack-worker.exe" in smoke and "bacpack.exe" in smoke


def test_the_windows_service_and_admin_command_run_the_renamed_interpreter_in_bundle_mode():
    ps1 = _install_ps1()
    assert r"$ServicePython = Join-Path $InstallDir 'python\bacpack.exe'" in ps1
    assert "{{PYTHON}}" in _template()
    # Source and venv installs keep the venv's Python.
    assert "$ServicePython = $venvPy" in ps1


def test_setproctitle_is_a_pinned_dependency():
    import tomllib

    data = tomllib.loads((PACKAGING.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert any(dep.startswith("setproctitle") for dep in data["project"]["dependencies"])
    pins = (PACKAGING / "constraints.txt").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("setproctitle==") for line in pins)


@pytest.mark.parametrize(
    ("sys_platform", "machine", "wanted"),
    [
        ("win32", "AMD64", True),
        ("win32", "x86", True),
        ("linux", "x86_64", True),
        ("linux", "aarch64", True),
        ("linux", "ppc64le", True),
        ("linux", "riscv64", True),
        # No setproctitle wheel here: pip would need a compiler, and a
        # cosmetic process name must never fail the install.
        ("linux", "armv7l", False),
        ("linux", "i686", False),
        ("linux", "s390x", False),
        ("win32", "ARM64", False),
    ],
)
def test_setproctitle_is_asked_for_only_where_it_has_a_wheel(sys_platform, machine, wanted):
    import tomllib

    from packaging.requirements import Requirement

    data = tomllib.loads((PACKAGING.parent / "pyproject.toml").read_text(encoding="utf-8"))
    req = next(
        Requirement(dep) for dep in data["project"]["dependencies"] if dep.startswith("setproctitle")
    )
    assert req.marker is not None
    assert req.marker.evaluate({"sys_platform": sys_platform, "platform_machine": machine}) is wanted


def test_install_sh_refuses_a_privileged_port_without_the_capability_before_changing_anything():
    sh = _install_sh()
    assert "capability_granted() {" in sh
    assert "/etc/systemd/system/bacpack.service.d" in sh
    call = sh.index("(( PORT < 1024 ))")
    assert sh.index("must be 1-65535") < call < sh.index("systemctl stop bacpack.service")
    assert call < sh.index("# --- user, data directory")
    block = sh[call:call + 900]
    assert "capability_granted" in block
    for needle in ("below 1024", "mkdir -p /etc/systemd/system/bacpack.service.d", "override.conf", "AmbientCapabilities=CAP_NET_BIND_SERVICE", "--port"):
        assert needle in block
    # die() echoes without -e, so the user sees one copyable printf line.
    printf_line = next(line for line in block.splitlines() if "printf '[Service]" in line)
    assert "printf '[Service]\\nAmbientCapabilities=CAP_NET_BIND_SERVICE\\n' | sudo tee" in printf_line


def test_install_sh_closing_note_gives_the_capability_step_before_port_443():
    sh = _install_sh()
    note = sh[sh.index('echo "- For port 443'):]
    note = note[:note.index("SECURITY.md")]
    assert "bacpack.service.d" in note and "override.conf" in note and "--port 443" in note
    assert note.index("bacpack.service.d") < note.index("--port 443")
    assert "systemctl edit" not in sh


def test_install_sh_capability_check_ignores_case_and_honours_a_lowered_unprivileged_port_start():
    sh = _install_sh()
    body = sh[sh.index("capability_granted() {"):sh.index("if (( PORT < 1024 ))")]
    assert "grep -qsiE" in body and "ambientcapabilities" in body.lower()
    assert "/proc/sys/net/ipv4/ip_unprivileged_port_start" in sh[sh.index("if (( PORT < 1024 ))") - 600:sh.index("if (( PORT < 1024 ))") + 400]
