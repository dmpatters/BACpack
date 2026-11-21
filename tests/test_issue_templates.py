from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / ".github" / "ISSUE_TEMPLATE"


def _read(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_bug_report_asks_for_the_diagnostics_file_the_client_and_the_steps():
    text = _read("bug_report.yml")
    assert "name: Bug report" in text
    for needed in (
        "About page", "Download diagnostics", "BACnet client", "Steps to reproduce", "What did you expect",
    ):
        assert needed in text


def test_feature_request_asks_what_and_how_today():
    text = _read("feature_request.yml")
    assert "name: Feature request" in text
    assert "trying to do" in text
    assert "do it today" in text


def test_config_turns_blank_issues_off_and_links_the_security_report():
    text = _read("config.yml")
    assert "blank_issues_enabled: false" in text
    assert "Report a security vulnerability" in text
    assert "https://github.com/dmpatters/BACpack/security/advisories/new" in text


def test_every_form_has_a_body_with_required_fields_marked():
    for name in ("bug_report.yml", "feature_request.yml"):
        text = _read(name)
        assert "body:" in text
        assert "validations:" in text
        assert "required: true" in text


def test_the_diagnostics_field_is_optional_and_says_what_to_paste_when_the_ui_wont_open():
    import re

    text = _read("bug_report.yml")
    field = text[text.index("id: diagnostics"):]
    assert not re.search(r"required:\s*true", field)
    assert "service log" in field
