"""A change to the shipped frontend sources needs a rebuilt, committed SPA
(src/bacpack/web/spa is what FastAPI serves; there is no build step at run time).

Every commit reachable from HEAD that touches shipped frontend/src files (tests and
setupTests.ts aside) must be the newest commit touching the SPA, an ancestor of it,
or carry a non-empty `SPA-unchanged: <reason>` trailer, for a change whose rebuild
is byte-identical (a comment-only edit: the build strips comments, so there is
nothing to commit). Merge commits are skipped: git's own path simplification lists
the branch commits that brought a frontend change in, and a merge with no frontend
diff of its own has nothing to rebuild. Uncommitted edits don't count: the suite
also runs before the commit."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = [
    "frontend/src",
    ":(exclude,glob)**/*.test.ts",
    ":(exclude,glob)**/*.test.tsx",
    ":(exclude,glob)**/setupTests.ts",
]
SPA = ["src/bacpack/web/spa"]


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=check, encoding="utf-8"
    )


def _newest(root: Path, pathspec: list[str]) -> str:
    return _git(root, "log", "-1", "--format=%H", "--", *pathspec).stdout.strip()


def _trailer(root: Path, commit: str) -> str:
    return _git(
        root, "log", "-1", "--format=%(trailers:key=SPA-unchanged,valueonly)", commit
    ).stdout.strip()


def _remerge_diff(root: Path, commit: str) -> str | None:
    """A merge's own change to the shipped files (its conflict resolution), "" when
    it has none, None when this git cannot say (--remerge-diff needs git 2.36)."""
    shown = _git(root, "show", "--remerge-diff", "--format=", commit, "--", *SHIPPED, check=False)
    return shown.stdout if shown.returncode == 0 else None


def _unexplained(root: Path, spa: str) -> list[tuple[str, str]]:
    """(hash, subject) of the shipped-frontend commits after the SPA that lack the trailer.

    Merges are listed apart: a merge counts only when its own resolution changes the
    shipped files, and with a git too old for --remerge-diff that pass is skipped
    (the commits that brought the change in are still checked)."""
    since = [f"^{spa}"] if spa else []
    out = _git(
        root, "log", "--no-merges", "-z",
        "--format=%H%x1f%s%x1f%(trailers:key=SPA-unchanged,valueonly)",
        "HEAD", *since, "--", *SHIPPED,
    ).stdout
    found = []
    for record in filter(None, out.split("\0")):
        commit, subject, trailer = (record.strip("\n") + "\x1f\x1f").split("\x1f")[:3]
        if not trailer.strip():
            found.append((commit, subject))
    merges = _git(root, "log", "--merges", "--format=%H%x1f%s", "HEAD", *since, "--", *SHIPPED).stdout
    for line in merges.splitlines():
        commit, _, subject = line.partition("\x1f")
        if _remerge_diff(root, commit) and not _trailer(root, commit):
            found.append((commit, subject))
    return found


def stale_spa(root: Path) -> str | None:
    """Why the committed SPA is older than the frontend sources, or None."""
    spa = _newest(root, SPA)
    offenders = _unexplained(root, spa)
    if not offenders:
        return None
    listed = "; ".join(f"{commit[:8]} {subject}" for commit, subject in offenders)
    if not spa:
        return f"frontend/src was committed ({listed}) and src/bacpack/web/spa never was"
    return (
        f"frontend/src changed after the last SPA commit {spa[:8]} in: {listed}. Rebuild "
        "(npm run build) and commit src/bacpack/web/spa, or, when the rebuild is "
        "byte-identical, add the trailer `SPA-unchanged: <reason>` to each of those "
        "commits (it goes in the last paragraph of the message)"
    )


def _usable(root: Path) -> bool:
    if shutil.which("git") is None or not (root / ".git").exists():
        return False
    if _git(root, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        return False
    return _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() != "true"


@pytest.mark.skipif(not _usable(ROOT), reason="needs a full git checkout")
def test_the_committed_spa_is_not_older_than_the_frontend_sources():
    problem = stale_spa(ROOT)
    assert problem is None, problem


# --- the check itself, on small repositories ---------------------------------

APP = "frontend/src/App.tsx"
INDEX = "src/bacpack/web/spa/index.html"


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def _commit(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")


def test_a_repository_without_frontend_changes_is_fine(repo):
    _commit(repo, {"README": "x"})
    assert stale_spa(repo) is None


def test_frontend_then_spa_passes(repo):
    _commit(repo, {APP: "1"})
    _commit(repo, {INDEX: "a"})
    assert stale_spa(repo) is None


def test_frontend_and_spa_in_one_commit_passes(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    assert stale_spa(repo) is None


def test_frontend_after_the_spa_fails(repo):
    _commit(repo, {APP: "1"})
    _commit(repo, {INDEX: "a"})
    _commit(repo, {APP: "2"})
    assert "rebuild" in (stale_spa(repo) or "")


def test_frontend_without_any_spa_fails(repo):
    _commit(repo, {APP: "1"})
    assert "never was" in (stale_spa(repo) or "")


@pytest.mark.parametrize("name", [
    "frontend/src/App.test.tsx", "frontend/src/api/http.test.ts", "frontend/src/setupTests.ts",
])
def test_tests_and_setup_changes_need_no_rebuild(repo, name):
    _commit(repo, {APP: "1", INDEX: "a"})
    _commit(repo, {name: "t"})
    assert stale_spa(repo) is None


def test_an_uncommitted_frontend_edit_does_not_count(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    (repo / APP).write_text("2", encoding="utf-8")
    assert stale_spa(repo) is None


# --- the SPA-unchanged trailer ----------------------------------------------

def _commit_msg(root: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


TRAILER = "comment-only\n\nSPA-unchanged: comment-only; the rebuilt SPA is byte-identical"


def test_a_trailered_frontend_commit_after_the_spa_passes(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _commit_msg(repo, {APP: "2"}, TRAILER)
    assert stale_spa(repo) is None


def test_a_trailer_with_no_value_does_not_exempt(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _commit_msg(repo, {APP: "2"}, "comments\n\nSPA-unchanged:")
    assert "rebuild" in (stale_spa(repo) or "")


def test_the_trailer_must_be_in_the_last_paragraph(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _commit_msg(repo, {APP: "2"}, "subj\n\nSPA-unchanged: x\n\nand then some prose")
    assert "rebuild" in (stale_spa(repo) or "")


def test_a_trailer_line_as_the_subject_does_not_exempt(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _commit_msg(repo, {APP: "2"}, "SPA-unchanged: x")
    assert "rebuild" in (stale_spa(repo) or "")


def test_every_commit_after_the_spa_needs_the_trailer(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _commit(repo, {APP: "2"})
    _commit_msg(repo, {APP: "3"}, TRAILER)
    assert "rebuild" in (stale_spa(repo) or "")


def test_the_untrailered_commit_is_named(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _commit_msg(repo, {APP: "2"}, "reword labels")
    problem = stale_spa(repo) or ""
    assert _newest(repo, SHIPPED)[:8] in problem
    assert "reword labels" in problem
    assert "SPA-unchanged" in problem


def test_trailered_commits_without_any_spa_pass(repo):
    _commit_msg(repo, {APP: "1"}, TRAILER)
    assert stale_spa(repo) is None


def test_a_frontend_commit_before_the_spa_needs_no_trailer(repo):
    _commit(repo, {APP: "1"})
    _commit(repo, {INDEX: "a"})
    _commit_msg(repo, {APP: "2"}, TRAILER)
    assert stale_spa(repo) is None


def test_a_merge_commit_without_its_own_frontend_diff_does_not_count(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _git(repo, "checkout", "-q", "-b", "side")
    _commit_msg(repo, {APP: "2"}, TRAILER)
    _git(repo, "checkout", "-q", "-")
    _commit_msg(repo, {"frontend/src/Other.tsx": "o"}, TRAILER)
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    assert stale_spa(repo) is None


def test_an_untrailered_commit_on_a_merged_branch_fails(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _git(repo, "checkout", "-q", "-b", "side")
    _commit_msg(repo, {APP: "2"}, "plain")
    _git(repo, "checkout", "-q", "-")
    _commit(repo, {"README": "x"})
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    assert "plain" in (stale_spa(repo) or "")


def _conflicted_merge(repo, resolution: str, message: str) -> None:
    """Both sides edit APP (each trailered); the merge's resolution is its own change."""
    _commit(repo, {APP: "1", INDEX: "a"})
    _git(repo, "checkout", "-q", "-b", "side")
    _commit_msg(repo, {APP: "side"}, TRAILER)
    _git(repo, "checkout", "-q", "-")
    _commit_msg(repo, {APP: "main"}, TRAILER)
    assert _git(repo, "merge", "--no-ff", "-m", message, "side", check=False).returncode != 0
    (repo / APP).write_text(resolution, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-edit", "-m", message)


def test_a_merge_whose_resolution_changes_the_frontend_needs_the_trailer(repo):
    _conflicted_merge(repo, "resolved", "Merge side")
    problem = stale_spa(repo) or ""
    assert "rebuild" in problem
    assert "Merge side" in problem


def test_a_trailered_merge_resolution_passes(repo):
    _conflicted_merge(repo, "resolved", "Merge side\n\nSPA-unchanged: comments only")
    assert stale_spa(repo) is None


def test_without_remerge_diff_the_merge_pass_is_skipped(repo, monkeypatch):
    import test_spa_committed as me  # noqa: F401  (the module under test, by its own name)
    monkeypatch.setattr(me, "_remerge_diff", lambda root, commit: None)
    _conflicted_merge(repo, "resolved", "Merge side")
    assert stale_spa(repo) is None


def test_every_offender_is_listed(repo):
    _commit(repo, {APP: "1", INDEX: "a"})
    _commit_msg(repo, {APP: "2"}, "first change")
    _commit_msg(repo, {APP: "3"}, "second change")
    problem = stale_spa(repo) or ""
    assert "first change" in problem and "second change" in problem
    assert "last paragraph" in problem
