#!/usr/bin/env python3
# Turns an upstream tenstorrent/ttsim release vX.Y.Z into the fork release vX.Y.Z-steps.N: the upstream tag commit
# with stepping-clock.patch applied on top. The candidate commit is a pure function of the upstream commit and the
# patch (fixed committer, committer date taken from the patch), so every job rebuilds the same SHA instead of passing
# commits between jobs. Published tags are the only state: a tag whose tree equals the candidate's is that release.
#
# Regenerate the patch from a commit with
#   git format-patch --no-signature --stdout -1 <commit> | sed 's/[ ]*$//' > .github/steps/stepping-clock.patch
# Stripping trailing spaces keeps the file within scripts/check_style.py; git apply reads an empty line as an empty
# context line.
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from typing import NoReturn

UPSTREAM_REPO = 'tenstorrent/ttsim'
UPSTREAM_URL = f'https://github.com/{UPSTREAM_REPO}'
PATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stepping-clock.patch')
CANDIDATE_DIR = 'candidate'
COMMITTER = {
    'GIT_COMMITTER_NAME': 'github-actions[bot]',
    'GIT_COMMITTER_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com',
}
# The libraries a release must carry; the multi-chip ones are published too when built
REQUIRED_LIBS = ['libttsim_wh.so', 'libttsim_wh_aarch64.so', 'libttsim_bh.so', 'libttsim_bh_aarch64.so']
VERSION = re.compile(r'v\d+\.\d+\.\d+')
STEPS_VERSION = re.compile(r'v\d+\.\d+\.\d+-steps\.\d+')

def die(msg: str) -> NoReturn:
    print(f'error: {msg}', file=sys.stderr)
    sys.exit(1)

def log(msg: str) -> None:
    print(msg, file=sys.stderr)

# Network commands get three attempts so a transient failure does not fail the run
def run(*cmd: str, cwd: str = '.', env: dict = None, attempts: int = 1, check: bool = True):
    for attempt in range(1, attempts + 1):
        result = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True)
        if result.returncode == 0 or attempt == attempts:
            break
        log(f'retrying after: {" ".join(cmd)}: {result.stderr.strip()}')
        time.sleep(10 * attempt)
    if check and result.returncode != 0:
        die(f'{" ".join(cmd)} failed:\n{result.stderr.strip()}')
    return result

def git(*args: str, cwd: str = '.') -> str:
    return run('git', *args, cwd=cwd).stdout.strip()

def gh_api(path: str, *args: str):
    return json.loads(run('gh', 'api', path, *args, attempts=3).stdout)

# All releases of repo, drafts included when the token may see them
def list_releases(repo: str) -> list:
    return [r for page in gh_api(f'repos/{repo}/releases?per_page=100', '--paginate', '--slurp') for r in page]

def version(tag: str) -> tuple:
    return tuple(int(x) for x in re.findall(r'\d+', tag))

# The highest published, non-prerelease upstream vX.Y.Z release, or the given one after checking it is such a release
def select_tag(tag: str) -> str:
    tags = [r['tag_name'] for r in list_releases(UPSTREAM_REPO)
            if not r['draft'] and not r['prerelease'] and VERSION.fullmatch(r['tag_name'])]
    if tag:
        if tag not in tags:
            die(f'{tag} is not a published vX.Y.Z release of {UPSTREAM_REPO}')
        return tag
    if not tags:
        die(f'{UPSTREAM_REPO} has no published vX.Y.Z release')
    return max(tags, key=version)

def resolve_upstream(tag: str) -> str:
    refs = run('git', 'ls-remote', UPSTREAM_URL, f'refs/tags/{tag}', f'refs/tags/{tag}^{{}}', attempts=3).stdout
    shas = {ref: sha for (sha, ref) in (line.split('\t') for line in refs.splitlines())}
    sha = shas.get(f'refs/tags/{tag}^{{}}') or shas.get(f'refs/tags/{tag}')
    if not sha:
        die(f'{UPSTREAM_REPO} has no tag {tag}')
    return sha

# Checks out the upstream commit in a worktree and applies the patch there, failing on anything but a clean apply
def make_candidate(upstream_sha: str) -> str:
    run('git', 'fetch', '--no-tags', UPSTREAM_URL, upstream_sha, attempts=3)
    git('worktree', 'add', '--detach', CANDIDATE_DIR, upstream_sha)
    applied = run('git', 'am', '--committer-date-is-author-date', PATCH, cwd=CANDIDATE_DIR, env=COMMITTER, check=False)
    if applied.returncode != 0:
        git('am', '--abort', cwd=CANDIDATE_DIR)
        if run('git', 'apply', '--check', '--reverse', PATCH, cwd=CANDIDATE_DIR, check=False).returncode == 0:
            die(f'upstream {upstream_sha} already contains the patch; retire or rewrite {PATCH}')
        die(f'the patch does not apply to upstream {upstream_sha}:\n{applied.stdout}{applied.stderr}')
    return git('rev-parse', 'HEAD', cwd=CANDIDATE_DIR)

def is_published(repo: str, tag: str) -> bool:
    # This endpoint returns published releases only, so a draft reads as 404 like a missing release
    result = run('gh', 'api', f'repos/{repo}/releases/tags/{tag}', attempts=3, check=False)
    if result.returncode == 0:
        return True
    if 'HTTP 404' in result.stderr:
        return False
    die(f'cannot read release {tag}: {result.stderr.strip()}')

# Prints the plan as GitHub step outputs; release_tag is empty when the candidate is already released
def plan(args) -> None:
    repo = os.environ['GITHUB_REPOSITORY']
    tag = select_tag(args.tag)
    upstream_sha = resolve_upstream(tag)
    candidate = make_candidate(upstream_sha)
    tree = git('rev-parse', f'{candidate}^{{tree}}')
    pattern = f'refs/tags/{tag}-steps.*'
    run('git', 'fetch', '--no-tags', f'https://github.com/{repo}', f'+{pattern}:{pattern}', attempts=3)
    numbers = sorted(int(m[1]) for t in git('tag', '--list', f'{tag}-steps.*').split()
                     if (m := re.fullmatch(rf'{re.escape(tag)}-steps\.(\d+)', t)))
    release_tag = f'{tag}-steps.{numbers[-1] + 1 if numbers else 1}'
    for n in numbers:
        steps_tag = f'{tag}-steps.{n}'
        if git('rev-parse', f'{steps_tag}^{{tree}}') != tree:
            continue
        if is_published(repo, steps_tag):
            log(f'{steps_tag} already releases upstream {tag} ({upstream_sha}) with this patch')
            release_tag = ''
        elif git('rev-parse', f'{steps_tag}^{{commit}}') == candidate:
            log(f'{steps_tag} was tagged but not published; resuming it')
            release_tag = steps_tag
        else:
            die(f'{steps_tag} holds the candidate source at another commit than {candidate} and is not published')
        break
    log(f'upstream {tag} ({upstream_sha}), candidate {candidate}, release {release_tag or "none"}')
    print(f'upstream_tag={tag}')
    print(f'upstream_sha={upstream_sha}')
    print(f'candidate={candidate}')
    print(f'release_tag={release_tag}')

def candidate(args) -> None:
    sha = make_candidate(args.upstream_sha)
    if sha != args.candidate:
        die(f'rebuilt candidate {sha} differs from the planned {args.candidate}')

def sha256(path: str) -> str:
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()

def publish(args) -> None:
    repo = os.environ['GITHUB_REPOSITORY']
    names = sorted(os.listdir(args.dist))
    for name in REQUIRED_LIBS:
        if name not in names or os.path.getsize(os.path.join(args.dist, name)) == 0:
            die(f'{name} is missing or empty')
    sums = ''.join(f'{sha256(os.path.join(args.dist, name))}  {name}\n' for name in names)
    with open(os.path.join(args.dist, 'SHA256SUMS'), 'w') as f:
        f.write(sums)
    names.append('SHA256SUMS')
    files = [os.path.join(args.dist, name) for name in names]

    candidate(args)
    # Without --force, the push fails rather than moves a tag that points elsewhere
    run('git', 'push', 'origin', f'{args.candidate}:refs/tags/{args.release_tag}', attempts=3)

    releases = list_releases(repo)
    release = next((r for r in releases if r['tag_name'] == args.release_tag), None)
    if release and not release['draft']:
        log(f'{args.release_tag} is already published; leaving it as is')
        return
    server = os.environ['GITHUB_SERVER_URL']
    notes = (
        f'Upstream [`{args.upstream_tag}`]({UPSTREAM_URL}/releases/tag/{args.upstream_tag}) (`{args.upstream_sha}`) '
        f'with `.github/steps/stepping-clock.patch` (blob `{git("hash-object", PATCH)}`), '
        f'committed as `{args.candidate}`.\n\n'
        f'Built, tested, and released by `{os.environ["GITHUB_SHA"]}` in '
        f'{server}/{repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}.\n\n'
        f'```text\n{sums}```\n'
    )
    # A draft left by an interrupted run is not public yet, so its assets and notes may be replaced
    if release:
        run('gh', 'release', 'upload', args.release_tag, *files, '--clobber', attempts=3)
        gh_api(f'repos/{repo}/releases/{release["id"]}', '-X', 'PATCH', '-f', f'body={notes}')
    else:
        run('gh', 'release', 'create', args.release_tag, *files, '--draft', '--verify-tag',
            '--title', args.release_tag, '--notes', notes)
        release = next(r for r in list_releases(repo) if r['tag_name'] == args.release_tag)

    release = gh_api(f'repos/{repo}/releases/{release["id"]}')
    uploaded = {a['name']: (a['size'], a['digest']) for a in release['assets']}
    expected = {os.path.basename(path): (os.path.getsize(path), f'sha256:{sha256(path)}') for path in files}
    if uploaded != expected:
        die(f'draft {args.release_tag} assets {uploaded} differ from the built {expected}')

    # A backfilled older version must not take the latest label from a newer one
    newest = max((r['tag_name'] for r in releases if not r['draft'] and STEPS_VERSION.fullmatch(r['tag_name'])),
                 key=version, default=None)
    make_latest = newest is None or version(args.release_tag) > version(newest)
    gh_api(f'repos/{repo}/releases/{release["id"]}', '-X', 'PATCH', '-F', 'draft=false',
           '-f', f'make_latest={str(make_latest).lower()}')
    log(f'published {args.release_tag}{" as latest" if make_latest else ""}')

def main() -> None:
    parser = argparse.ArgumentParser(description='Plan, rebuild, and publish fork releases of upstream ttsim tags.')
    commands = parser.add_subparsers(required=True)
    p = commands.add_parser('plan', help='pick the upstream tag and the release to make, as GitHub step outputs')
    p.add_argument('tag', nargs='?', default='', help='upstream vX.Y.Z tag; defaults to the highest release')
    p.set_defaults(func=plan)
    p = commands.add_parser('candidate', help=f'rebuild the planned candidate in ./{CANDIDATE_DIR}')
    p.add_argument('upstream_sha')
    p.add_argument('candidate')
    p.set_defaults(func=candidate)
    p = commands.add_parser('publish', help='tag the candidate and publish the built libraries in dist')
    for name in ('release_tag', 'upstream_tag', 'upstream_sha', 'candidate', 'dist'):
        p.add_argument(name)
    p.set_defaults(func=publish)
    args = parser.parse_args()
    args.func(args)

main()
