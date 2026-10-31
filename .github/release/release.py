#!/usr/bin/env python3
"""Release an upstream tenstorrent/ttsim vX.Y.Z as the fork release vX.Y.Z-steps.N.

A release is the upstream tag commit with stepping-clock.patch applied on top. The candidate commit is a pure function
of the upstream commit and the patch (fixed committer, committer date taken from the patch), so every job rebuilds the
same SHA instead of passing commits between jobs. Published tags are the only state: a tag whose tree equals the
candidate's is that release.

Regenerate the patch from a commit with

    git format-patch --no-signature --stdout -1 <commit> | sed 's/[ ]*$//' > .github/release/stepping-clock.patch

Stripping trailing spaces keeps the file within scripts/check_style.py; git apply reads an empty line as an empty
context line.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, TypedDict

UPSTREAM_REPO = "tenstorrent/ttsim"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_REPO}"
PATCH = Path(__file__).resolve().with_name("stepping-clock.patch")
CANDIDATE_DIR = "candidate"
COMMITTER = {
    "GIT_COMMITTER_NAME": "github-actions[bot]",
    "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
}
# The libraries a release must carry; the multi-chip ones are published too when built
REQUIRED_LIBS = ["libttsim_wh.so", "libttsim_wh_aarch64.so", "libttsim_bh.so", "libttsim_bh_aarch64.so"]
SUMS = "SHA256SUMS"
VERSION = re.compile(r"v\d+\.\d+\.\d+")
STEPS_VERSION = re.compile(r"v\d+\.\d+\.\d+-steps\.\d+")
NETWORK_ATTEMPTS = 3


class Asset(TypedDict):
    """An uploaded release asset as the GitHub API reports it."""

    name: str
    size: int
    digest: str | None


class Release(TypedDict):
    """The fields of a GitHub release that the controller reads."""

    id: int
    tag_name: str
    draft: bool
    prerelease: bool
    assets: list[Asset]


def die(msg: str) -> NoReturn:
    """Print an error and fail the job."""
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def log(msg: str) -> None:
    """Print progress to stderr, keeping stdout for step outputs."""
    print(msg, file=sys.stderr)


def run(
    *cmd: str, cwd: str = ".", env: dict[str, str] | None = None, attempts: int = 1, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run cmd, retrying network commands so a transient failure does not fail the run."""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            cmd, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True, check=False
        )
        if result.returncode == 0 or attempt == attempts:
            break
        log(f"retrying after: {' '.join(cmd)}: {result.stderr.strip()}")
        time.sleep(10 * attempt)
    if check and result.returncode != 0:
        die(f"{' '.join(cmd)} failed:\n{result.stderr.strip()}")
    return result


def git(*args: str, cwd: str = ".") -> str:
    """Run git and return its stripped output."""
    return run("git", *args, cwd=cwd).stdout.strip()


def gh_api(path: str, *args: str) -> Release:
    """Call the GitHub API through gh and decode the JSON response."""
    return json.loads(run("gh", "api", path, *args, attempts=NETWORK_ATTEMPTS).stdout)


def list_releases(repo: str) -> list[Release]:
    """List all releases of repo, drafts included when the token may see them."""
    path = f"repos/{repo}/releases?per_page=100"
    pages = json.loads(run("gh", "api", path, "--paginate", "--slurp", attempts=NETWORK_ATTEMPTS).stdout)
    return [r for page in pages for r in page]


def version(tag: str) -> tuple[int, ...]:
    """Return the numeric parts of tag for ordering."""
    return tuple(int(x) for x in re.findall(r"\d+", tag))


def select_tag(tag: str) -> str:
    """Return the highest published, non-prerelease upstream vX.Y.Z release, or tag after checking it is one."""
    tags = [
        r["tag_name"]
        for r in list_releases(UPSTREAM_REPO)
        if not r["draft"] and not r["prerelease"] and VERSION.fullmatch(r["tag_name"])
    ]
    if tag:
        if tag not in tags:
            die(f"{tag} is not a published vX.Y.Z release of {UPSTREAM_REPO}")
        return tag
    if not tags:
        die(f"{UPSTREAM_REPO} has no published vX.Y.Z release")
    return max(tags, key=version)


def resolve_upstream(tag: str) -> str:
    """Return the commit that the upstream tag points to."""
    refs = run(
        "git", "ls-remote", UPSTREAM_URL, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}", attempts=NETWORK_ATTEMPTS
    ).stdout
    shas = {ref: sha for sha, ref in (line.split("\t") for line in refs.splitlines())}
    sha = shas.get(f"refs/tags/{tag}^{{}}") or shas.get(f"refs/tags/{tag}")
    if not sha:
        die(f"{UPSTREAM_REPO} has no tag {tag}")
    return sha


def make_candidate(upstream_sha: str) -> str:
    """Check out upstream_sha in a worktree and apply the patch, failing on anything but a clean apply."""
    run("git", "fetch", "--no-tags", UPSTREAM_URL, upstream_sha, attempts=NETWORK_ATTEMPTS)
    git("worktree", "add", "--detach", CANDIDATE_DIR, upstream_sha)
    applied = run(
        "git", "am", "--committer-date-is-author-date", str(PATCH), cwd=CANDIDATE_DIR, env=COMMITTER, check=False
    )
    if applied.returncode != 0:
        git("am", "--abort", cwd=CANDIDATE_DIR)
        if run("git", "apply", "--check", "--reverse", str(PATCH), cwd=CANDIDATE_DIR, check=False).returncode == 0:
            die(f"upstream {upstream_sha} already contains the patch; retire or rewrite {PATCH}")
        die(f"the patch does not apply to upstream {upstream_sha}:\n{applied.stdout}{applied.stderr}")
    return git("rev-parse", "HEAD", cwd=CANDIDATE_DIR)


def is_published(repo: str, tag: str) -> bool:
    """Return whether tag is a public release; tokens with enough access may see drafts here too."""
    result = run("gh", "api", f"repos/{repo}/releases/tags/{tag}", attempts=NETWORK_ATTEMPTS, check=False)
    if result.returncode == 0:
        return not json.loads(result.stdout)["draft"]
    if "HTTP 404" in result.stderr:
        return False
    die(f"cannot read release {tag}: {result.stderr.strip()}")


def plan(args: argparse.Namespace) -> None:
    """Print the plan as GitHub step outputs; release_tag is empty when the candidate is already released."""
    repo = os.environ["GITHUB_REPOSITORY"]
    tag = select_tag(args.tag)
    upstream_sha = resolve_upstream(tag)
    candidate = make_candidate(upstream_sha)
    tree = git("rev-parse", f"{candidate}^{{tree}}")
    pattern = f"refs/tags/{tag}-steps.*"
    run("git", "fetch", "--no-tags", f"https://github.com/{repo}", f"+{pattern}:{pattern}", attempts=NETWORK_ATTEMPTS)
    numbers = sorted(
        int(m[1])
        for t in git("tag", "--list", f"{tag}-steps.*").split()
        if (m := re.fullmatch(rf"{re.escape(tag)}-steps\.(\d+)", t))
    )
    release_tag = f"{tag}-steps.{numbers[-1] + 1 if numbers else 1}"
    for n in numbers:
        steps_tag = f"{tag}-steps.{n}"
        # Every fork tag is one patch commit on its upstream base. Refuse a moved upstream tag.
        if git("rev-parse", f"{steps_tag}^") != upstream_sha:
            die(f"upstream {tag} no longer matches the base of {steps_tag}")
        if git("rev-parse", f"{steps_tag}^{{tree}}") != tree:
            continue
        if is_published(repo, steps_tag):
            log(f"{steps_tag} already releases upstream {tag} ({upstream_sha}) with this patch")
            release_tag = ""
        elif git("rev-parse", f"{steps_tag}^{{commit}}") == candidate:
            log(f"{steps_tag} was tagged but not published; resuming it")
            release_tag = steps_tag
        else:
            die(f"{steps_tag} holds the candidate source at another commit than {candidate} and is not published")
        break
    log(f"upstream {tag} ({upstream_sha}), candidate {candidate}, release {release_tag or 'none'}")
    print(f"upstream_tag={tag}")
    print(f"upstream_sha={upstream_sha}")
    print(f"candidate={candidate}")
    print(f"release_tag={release_tag}")


def candidate(args: argparse.Namespace) -> None:
    """Rebuild the planned candidate and check that its SHA matches the plan."""
    sha = make_candidate(args.upstream_sha)
    if sha != args.candidate:
        die(f"rebuilt candidate {sha} differs from the planned {args.candidate}")


def sha256(path: Path) -> str:
    """Return the hex SHA-256 of the file at path."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_assets(release: Release, files: list[Path]) -> None:
    """Fail unless the release assets match files by name, size, and digest."""
    uploaded = {a["name"]: (a["size"], a.get("digest")) for a in release["assets"]}
    expected = {path.name: (path.stat().st_size, f"sha256:{sha256(path)}") for path in files}
    if uploaded != expected:
        die(f"release {release['tag_name']} assets {uploaded} differ from the built {expected}")


def write_sums(dist: Path) -> tuple[list[Path], str]:
    """Check the required libraries, write SHA256SUMS, and return every file to upload with the sums."""
    libs = sorted(path for path in dist.iterdir() if path.name != SUMS)
    for name in REQUIRED_LIBS:
        path = dist / name
        if not path.is_file() or path.stat().st_size == 0:
            die(f"{name} is missing or empty")
    sums = "".join(f"{sha256(path)}  {path.name}\n" for path in libs)
    (dist / SUMS).write_bytes(sums.encode())
    return [*libs, dist / SUMS], sums


def publish(args: argparse.Namespace) -> None:
    """Tag the candidate and publish the built libraries, resuming a draft left by an interrupted run."""
    repo = os.environ["GITHUB_REPOSITORY"]
    files, sums = write_sums(Path(args.dist))

    candidate(args)
    # Without --force, the push fails rather than moves a tag that points elsewhere
    run("git", "push", "origin", f"{args.candidate}:refs/tags/{args.release_tag}", attempts=NETWORK_ATTEMPTS)

    releases = list_releases(repo)
    release = next((r for r in releases if r["tag_name"] == args.release_tag), None)
    if release and not release["draft"]:
        verify_assets(release, files)
        log(f"{args.release_tag} is already published; leaving it as is")
        return
    server = os.environ["GITHUB_SERVER_URL"]
    patch_path = PATCH.relative_to(Path(__file__).resolve().parents[2])
    notes = (
        f"Upstream [`{args.upstream_tag}`]({UPSTREAM_URL}/releases/tag/{args.upstream_tag}) (`{args.upstream_sha}`) "
        f"with `{patch_path.as_posix()}` (blob `{git('hash-object', str(PATCH))}`), "
        f"committed as `{args.candidate}`.\n\n"
        f"Built, tested, and released by `{os.environ['GITHUB_SHA']}` in "
        f"{server}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}.\n\n"
        f"```text\n{sums}```\n"
    )
    paths = [str(path) for path in files]
    # A draft left by an interrupted run is not public yet, so its assets and notes may be replaced
    if release:
        run("gh", "release", "upload", args.release_tag, *paths, "--clobber", attempts=NETWORK_ATTEMPTS)
        gh_api(f"repos/{repo}/releases/{release['id']}", "-X", "PATCH", "-f", f"body={notes}")
    else:
        run(
            "gh",
            "release",
            "create",
            args.release_tag,
            *paths,
            "--draft",
            "--verify-tag",
            "--title",
            args.release_tag,
            "--notes",
            notes,
        )
        release = next(r for r in list_releases(repo) if r["tag_name"] == args.release_tag)

    release = gh_api(f"repos/{repo}/releases/{release['id']}")
    verify_assets(release, files)

    # A backfilled older version must not take the latest label from a newer one
    newest = max(
        (r["tag_name"] for r in releases if not r["draft"] and STEPS_VERSION.fullmatch(r["tag_name"])),
        key=version,
        default=None,
    )
    make_latest = newest is None or version(args.release_tag) > version(newest)
    gh_api(
        f"repos/{repo}/releases/{release['id']}",
        "-X",
        "PATCH",
        "-F",
        "draft=false",
        "-f",
        f"make_latest={str(make_latest).lower()}",
    )
    log(f"published {args.release_tag}{' as latest' if make_latest else ''}")


def main() -> None:
    """Parse the command line and run the selected command."""
    parser = argparse.ArgumentParser(description="Plan, rebuild, and publish fork releases of upstream ttsim tags.")
    commands = parser.add_subparsers(required=True)
    p = commands.add_parser("plan", help="pick the upstream tag and the release to make, as GitHub step outputs")
    p.add_argument("tag", nargs="?", default="", help="upstream vX.Y.Z tag; defaults to the highest release")
    p.set_defaults(func=plan)
    p = commands.add_parser("candidate", help=f"rebuild the planned candidate in ./{CANDIDATE_DIR}")
    p.add_argument("upstream_sha")
    p.add_argument("candidate")
    p.set_defaults(func=candidate)
    p = commands.add_parser("publish", help="tag the candidate and publish the built libraries in dist")
    for name in ("release_tag", "upstream_tag", "upstream_sha", "candidate", "dist"):
        p.add_argument(name)
    p.set_defaults(func=publish)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
