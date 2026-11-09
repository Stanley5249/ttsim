"""Release decisions and interrupted publishing, tested without writing to GitHub."""

import argparse
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import release as ci

TAG = "v1.0.0-steps.1"


def release(tag: str, *, draft: bool = False, prerelease: bool = False) -> ci.Release:
    """Return a release record with no assets."""
    return {"id": 1, "tag_name": tag, "draft": draft, "prerelease": prerelease, "assets": []}


@pytest.mark.parametrize("draft", [False, True])
def test_draft_is_not_published(monkeypatch: pytest.MonkeyPatch, *, draft: bool) -> None:
    """Only a public release counts as published."""
    monkeypatch.setattr(ci, "run", Mock(return_value=Mock(returncode=0, stdout=json.dumps({"draft": draft}))))
    assert ci.is_published("owner/repo", TAG) is not draft


def test_api_failure_is_not_a_missing_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server error must fail instead of reading as a missing release."""
    monkeypatch.setattr(ci, "run", Mock(return_value=Mock(returncode=1, stderr="HTTP 503")))
    with pytest.raises(SystemExit):
        ci.is_published("owner/repo", TAG)


def test_highest_version_ignores_draft_and_prerelease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Versions compare numerically, and drafts and prereleases are skipped."""
    releases = [
        release("v1.9.0"),
        release("v1.10.0"),
        release("v2.0.0", draft=True),
        release("v3.0.0", prerelease=True),
    ]
    monkeypatch.setattr(ci, "list_releases", Mock(return_value=releases))
    assert ci.select_tag("") == "v1.10.0"


def test_patch_selection() -> None:
    """v1.12.1 takes its own form of the patch, every other version takes unit-tick.patch, and each one exists."""
    assert ci.patch_for("v1.12.1").name == "unit-tick-v1.12.1.patch"
    for tag in ("v1.12.0", "v1.15.1", "v1.16.0"):
        assert ci.patch_for(tag) == ci.PATCH
    assert all(patch.is_file() for patch in [ci.PATCH, *ci.PATCHES.values()])


@pytest.mark.parametrize("tag", ["v1.12.1", "v1.16.0"])
def test_candidate_applies_the_patch_for_its_tag(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    """The candidate a build job rebuilds applies the patch selected by the upstream tag."""
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(ci, "run", run)
    monkeypatch.setattr(ci, "git", Mock(return_value="candidate"))
    ci.candidate(argparse.Namespace(upstream_tag=tag, upstream_sha="upstream", candidate="candidate"))
    applied = [c.args for c in run.call_args_list if c.args[:2] == ("git", "am")]
    assert applied == [("git", "am", "--committer-date-is-author-date", str(ci.patch_for(tag)))]


def test_moved_upstream_tag_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fork tag whose base differs from the current upstream tag stops the plan."""

    def git(*args: str) -> str:
        if args[0] == "tag":
            return TAG
        if args[1].endswith("^{tree}"):
            return "tree"
        return "original-upstream"

    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(ci, "select_tag", Mock(return_value="v1.0.0"))
    monkeypatch.setattr(ci, "resolve_upstream", Mock(return_value="moved-upstream"))
    monkeypatch.setattr(ci, "make_candidate", Mock(return_value="candidate"))
    monkeypatch.setattr(ci, "run", Mock())
    monkeypatch.setattr(ci, "git", git)
    with pytest.raises(SystemExit):
        ci.plan(argparse.Namespace(tag=""))


@pytest.mark.parametrize(
    ("published", "tree", "commit", "expected"),
    [
        pytest.param(True, "tree", "candidate", "", id="published-release-of-this-tree-needs-none"),
        pytest.param(False, "tree", "candidate", "v1.12.1-steps.1", id="tagged-unpublished-candidate-resumes"),
        pytest.param(True, "old-tree", "old", "v1.12.1-steps.2", id="changed-tree-takes-the-next-suffix"),
    ],
)
def test_plan_matches_tags_by_tree(  # noqa: PLR0913  # pytest parameters
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    published: bool,
    tree: str,
    commit: str,
    expected: str,
) -> None:
    """A fork tag whose tree equals the candidate's is that release; another tree takes the next suffix."""

    def git(*args: str) -> str:
        if args[0] == "tag":
            return "v1.12.1-steps.1"
        ref = args[1]
        if ref == "candidate^{tree}":
            return "tree"
        return {"v1.12.1-steps.1^": "upstream", "v1.12.1-steps.1^{tree}": tree, "v1.12.1-steps.1^{commit}": commit}[ref]

    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(ci, "select_tag", Mock(return_value="v1.12.1"))
    monkeypatch.setattr(ci, "resolve_upstream", Mock(return_value="upstream"))
    monkeypatch.setattr(ci, "make_candidate", Mock(return_value="candidate"))
    monkeypatch.setattr(ci, "run", Mock())
    monkeypatch.setattr(ci, "git", git)
    monkeypatch.setattr(ci, "is_published", Mock(return_value=published))
    ci.plan(argparse.Namespace(tag=""))
    assert f"release_tag={expected}\n" in capsys.readouterr().out


@pytest.mark.parametrize("upstream_tag", ["v1.0.0", "v1.12.1"])
@pytest.mark.parametrize(
    ("draft", "valid_assets", "newer_release"),
    [
        pytest.param(True, True, True, id="resume-draft-without-taking-latest-from-newer-version"),
        pytest.param(True, False, False, id="bad-upload-never-publishes"),
        pytest.param(False, False, False, id="public-release-with-changed-assets-fails-without-overwrite"),
    ],
)
def test_publish(  # noqa: PLR0913  # pytest parameters
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    upstream_tag: str,
    *,
    draft: bool,
    valid_assets: bool,
    newer_release: bool,
) -> None:
    """Publishing resumes drafts, verifies uploads, and never touches a public release."""
    for name in ci.REQUIRED_LIBS:
        (tmp_path / name).write_bytes(b"library")
    # A rerun must regenerate this file, not include its own previous hash
    (tmp_path / ci.SUMS).write_text("stale")
    record = release(TAG, draft=draft)
    releases = [record]
    if newer_release:
        releases.append(release("v2.0.0-steps.1"))

    def api(_path: str, *args: str) -> ci.Release:
        if args:
            return record
        record["assets"] = [
            {"name": p.name, "size": p.stat().st_size, "digest": f"sha256:{ci.sha256(p)}"} for p in tmp_path.iterdir()
        ]
        if not valid_assets:
            record["assets"][0]["digest"] = "sha256:wrong"
        return record

    # For a public release, list_releases supplies its assets directly
    api("metadata")
    for key, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_SHA": "control",
        "GITHUB_RUN_ID": "1",
    }.items():
        monkeypatch.setenv(key, value)
    run, call = Mock(), Mock(side_effect=api)
    monkeypatch.setattr(ci, "candidate", Mock())
    monkeypatch.setattr(ci, "run", run)
    monkeypatch.setattr(ci, "git", Mock(return_value="patch"))
    monkeypatch.setattr(ci, "list_releases", Mock(return_value=releases))
    monkeypatch.setattr(ci, "gh_api", call)
    args = argparse.Namespace(
        dist=str(tmp_path), upstream_tag=upstream_tag, upstream_sha="upstream", candidate="candidate", release_tag=TAG
    )

    if valid_assets:
        ci.publish(args)
    else:
        with pytest.raises(SystemExit):
            ci.publish(args)

    publish_calls = [c for c in call.call_args_list if "draft=false" in c.args]
    if draft and valid_assets:
        assert len(publish_calls) == 1
        assert f"make_latest={str(not newer_release).lower()}" in publish_calls[0].args
        assert any(c.args[:3] == ("gh", "release", "upload") for c in run.call_args_list)
        assert any(
            f"`.github/release/{ci.patch_for(upstream_tag).name}` (blob `patch`)" in arg
            for c in call.call_args_list
            for arg in c.args
        )
    else:
        assert publish_calls == []
    assert f"  {ci.SUMS}" not in (tmp_path / ci.SUMS).read_text()
