# Automatic upstream releases

The Release workflow checks the highest published, non-prerelease upstream
`vX.Y.Z` release daily at 22:17 UTC. It does not backfill skipped releases.
A manual run can select an older upstream release if the patch applies to it.
There is no dependency on tt-metal, and no approval gate for routine releases.
Select `validate_only` on a manual run to build the candidate even when already
released, without creating tags or releases. Scheduled runs always publish new
versions after validation.

`release.py` selects a version and applies `stepping-clock.patch`. Every build
job reconstructs the candidate and checks its commit SHA. Fixed commit metadata
makes this reproducible without pushing temporary branches. Build jobs have
read access only. The publishing job runs controller code, never candidate code.

The existing four OS/CPU build jobs run style and wall-clock behavior checks.
Ubuntu 24.04 artifacts supply the release libraries. Publishing creates a draft,
checks asset sizes and hashes, then makes it public automatically. Old-version
backfills do not take the latest label from a newer version.

Fork tags are one patch commit on the upstream base. A changed upstream tag is
an error. Tags are never moved and public assets are never overwritten. Drafts
can be resumed after interruption. Matching published source is skipped, while
changed patch output receives the next `steps.N`. No separate state store is
needed. Network operations retry; deterministic failures recur on the next
schedule until the controller or patch is fixed. Actions reports failures.

Regenerate the patch using the command at the top of `release.py`. Versions for
which this patch conflicts are not released; support requires adapting the
patch. Do not suppress simulator error checks to make a release pass.

Run controller regression tests with `python3 .github/steps/test_release.py`.
Run the behavioral check with
`python3 .github/steps/test_wall_clock.py /absolute/path/to/libttsim.so`.
The check exercises only simulator libraries, not hardware or model weights.

Deployment limits: Actions must be enabled on the fork and the workflow must
be present on its default branch. GitHub may disable scheduled workflows after
60 days of repository inactivity; this workflow cannot recover from its own
disablement. Tag creation with GITHUB_TOKEN may be rejected when candidate
history changes workflow files. Validate token permissions in the first real
release run before relying on unattended publication. Local regression tests
mock GitHub writes; they do not prove these platform permissions.
