"""The Windows setup wizard (packaging/windows/bacpack.iss), the wrapper that
uninstalls, and the install pages of the docs. The wizard's behaviour is checked
live on the VMs; these read the scripts for what a review found wrong."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def _iss() -> str:
    return (PACKAGING / "windows" / "bacpack.iss").read_text(encoding="utf-8")


def _install_ps1() -> str:
    return (PACKAGING / "install.ps1").read_text(encoding="utf-8")


def _doc(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _routine(text: str, header: str) -> str:
    """The Pascal routine that starts at `header`, up to the next top-level routine."""
    start = text.index(header)
    following = re.search(r"^(?:function|procedure) ", text[start + len(header):], re.MULTILINE)
    return text[start:start + len(header) + following.start()] if following else text[start:]


# --- a failed install stops setup (Abort in ssPostInstall does not) ----------------


def test_the_install_runs_in_prepare_to_install_which_stops_setup_with_exit_code_7():
    iss = _iss()
    prepare = _routine(iss, "function PrepareToInstall")
    assert "RunInstall" in prepare and "Result := Report" in prepare
    # Setup ignores Abort in CurStepChanged(ssPostInstall): nothing may be installed there.
    step = _routine(iss, "procedure CurStepChanged") if "procedure CurStepChanged" in iss else ""
    assert "ssPostInstall" not in step and "Abort" not in step
    # The bundle is extracted on demand (dontcopy), keeping its folders.
    assert "ExtractTemporaryFiles('*')" in prepare
    assert "dontcopy" in iss and "recursesubdirs" in iss


def test_the_wizard_follows_the_script_through_its_own_output_not_an_exit_file():
    iss = _iss()
    assert "ExecAndLogOutput(" in iss and "ewWaitUntilTerminated" in iss
    for gone in ("PeekMessage", ".exit", "PumpMessages", "setup_run.ps1"):
        assert gone not in iss
    assert not (PACKAGING / "windows" / "setup_run.ps1").exists()
    # install.ps1 runs as its own PowerShell, as the zip's install does (stderr inside it
    # is not turned into an error by a redirect in a parent script).
    assert "-NoProfile" in iss and "-ExecutionPolicy Bypass -File" in iss and "install.ps1" in iss


def test_the_exit_code_of_a_failed_install_is_documented_as_7():
    assert "exit code 7" in _doc("README.md")
    assert "exit code 5" not in _doc("README.md")


# --- silent installs ----------------------------------------------------------------


def test_the_docs_say_to_add_suppressmsgboxes_to_a_silent_install():
    assert "/VERYSILENT /SUPPRESSMSGBOXES" in _doc("README.md")


def test_the_port_parameter_takes_plain_digits_only():
    setup = _routine(_iss(), "function InitializeSetup")
    assert "IsDigits(Given)" in setup
    assert "function IsDigits" in _iss()


# --- downgrade guard ----------------------------------------------------------------


def test_the_downgrade_guard_reads_the_installed_package_and_the_64_bit_registry():
    iss = _iss()
    assert "bacpack-*.dist-info" in iss
    assert r"python\Lib\site-packages" in iss and r".venv\Lib\site-packages" in iss
    assert "HKLM64" in iss and "HKLM," not in iss.replace("HKLM64,", "")
    setup = _routine(iss, "function InitializeSetup")
    assert "Upgrading guide" in setup


def test_the_upgrade_failure_text_does_not_promise_the_previous_version_is_running():
    prepare = _routine(_iss(), "function PrepareToInstall")
    assert "before BACpack''s files were replaced" in prepare
    assert "again where that is possible" not in prepare


# --- the login pages ----------------------------------------------------------------


def test_the_wizard_checks_the_login_with_the_servers_rules():
    iss = _iss()
    check = _routine(iss, "function CheckLogin")
    assert "64" in check and "256" in check and "< 12" in check  # username and password lengths
    assert "Chr(34)" in check or "'\"'" in check  # a double quote
    for char in "&|<>^%!":
        assert f"'{char}'" in check or char in check
    assert "the same as the username" in check
    assert "CheckLogin(" in _routine(iss, "function NextButtonClick")


def test_kept_data_keeps_its_login_so_the_login_pages_are_skipped():
    iss = _iss()
    assert "HadData" in iss
    skip = _routine(iss, "function ShouldSkipPage")
    assert "HadData" in skip and "HadInstall" in skip
    assert r"{commonappdata}\BACpack\bacpack.db" in iss


def test_the_first_visit_hint_follows_the_logins_state():
    ps1 = _install_ps1()
    hint = ps1[ps1.index("Next: open the web UI and create a login") - 1100:ps1.index("Next: open the web UI and create a login")]
    assert "not chosen yet" in hint


# --- smaller fixes ------------------------------------------------------------------


def test_the_install_log_is_written_as_utf8_and_cancel_is_off_while_it_runs():
    iss = _iss()
    assert "SaveStringsToUTF8File" in iss
    prepare = _routine(iss, "function PrepareToInstall")
    assert "WizardForm.CancelButton.Enabled := False" in prepare
    assert "WizardForm.CancelButton.Enabled := True" in prepare


def test_a_failed_uninstall_aborts_so_the_entry_stays_for_a_retry():
    uninstall = _iss()[_iss().index("procedure CurUninstallStepChanged"):]
    failed = uninstall[uninstall.index("Code <> 0"):]
    assert "Abort;" in failed


def test_install_ps1_ends_with_an_explicit_exit_0():
    assert _install_ps1().rstrip().endswith("exit 0")


# --- bacpack-admin.cmd uninstall, run for real in one cmd session -------------------


def _wrapper_cmd_lines() -> list[str]:
    ps1 = _install_ps1()
    start = ps1.index("$AdminCmd = Join-Path $InstallDir 'bacpack-admin.cmd'")
    block = ps1[start:ps1.index("Set-Content $AdminCmd")]
    lines = re.findall(r"^\s*'(.*)',?\s*$", block, re.MULTILINE)
    return [line for line in lines if not line.startswith("#")]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe and PowerShell")
def test_uninstall_can_be_run_twice_in_one_cmd_session_and_returns_the_scripts_exit_code(tmp_path):
    installed = tmp_path / "inst"
    installed.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    wrapper = installed / "bacpack-admin.cmd"
    wrapper.write_text("\r\n".join(_wrapper_cmd_lines() + ["echo reached the python line"]) + "\r\n", encoding="ascii")
    marker = tmp_path / "ran.txt"
    (installed / "install.ps1").write_text(
        f"Add-Content -LiteralPath '{marker}' -Value \"ran $args\"\nexit 3\n", encoding="ascii"
    )
    # One cmd process, so what the first run leaves in the environment is seen by the second;
    # delayed expansion reads the exit code after each run, not when the line is parsed.
    command = f'"{wrapper}" uninstall --purge & echo rc1=!errorlevel! & "{wrapper}" uninstall & echo rc2=!errorlevel!'
    # A single string: cmd strips the outer quotes of /c "..." and keeps the inner ones.
    done = subprocess.run(
        f'cmd.exe /v:on /c "{command}"', capture_output=True, text=True, env={**os.environ, "TEMP": str(temp)}
    )
    out = done.stdout + done.stderr
    assert "rc1=3" in out and "rc2=3" in out, out
    assert "The system cannot find" not in out
    assert "reached the python line" not in out
    runs = marker.read_text().splitlines()
    assert len(runs) == 2 and "-Uninstall" in runs[0] and "-Purge" in runs[0] and "-Purge" not in runs[1]
    assert not list(temp.glob("bacpack-uninstall.ps1"))


def test_the_wrapper_hand_off_needs_no_environment_variable():
    assert "BP_IN_TEMP" not in _install_ps1()


# --- the docs -----------------------------------------------------------------------


def test_the_getting_started_guide_leads_with_the_setup():
    first = _doc("docs/guide/getting-started.md").split("2. Create a login.")[0]
    assert "setup.exe" in first


def test_the_upgrading_guide_covers_the_setup_and_its_refusal_to_go_back():
    text = _doc("docs/guide/upgrading.md")
    assert "setup.exe" in text and "refuses" in text.split("## Going back")[1].lower()


def test_the_architecture_page_mentions_the_setup():
    assert "setup.exe" in _doc("docs/architecture.md")


def test_the_uninstall_command_is_shown_so_that_powershell_can_run_it():
    for path in ("README.md", "docs/guide/uninstalling.md"):
        text = _doc(path)
        assert "Command Prompt" in text, path


def test_the_uninstall_guide_says_a_setup_install_is_removed_from_installed_apps():
    text = _doc("docs/guide/uninstalling.md")
    assert "Installed apps entry" in text and "Start menu" in text


def test_the_windows_readme_says_how_the_bundles_executables_are_signed():
    text = _doc("packaging/windows/README.md")
    assert "signature" in text.lower()
