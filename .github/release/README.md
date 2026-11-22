# Automatic upstream releases

The Release workflow checks the highest published, non-prerelease upstream
`vX.Y.Z` release daily at 22:17 UTC. It does not backfill skipped releases.
A manual run can select an older upstream release if the patch applies to it.
There is no dependency on tt-metal, and no approval gate for routine releases.
Select `validate_only` on a manual run to build the candidate even when already
released, without creating tags or releases. Scheduled runs always publish new
versions after validation.

`release.py` selects a version and applies `unit-tick.patch`. A version it
does not fit, such as `v1.12.1`, which predates the eth busy steps, can no
longer be released; its published builds stay valid. Every build
job reconstructs the candidate and checks its commit SHA. Fixed commit metadata
makes this reproducible without pushing temporary branches. Build jobs have
read access only. The publishing job runs controller code, never candidate code.

The patch steps each Tensix tile and each eth core once per clock. Upstream
clocks a busy tile repeatedly within one clock and gives a busy eth core
several steps, so a core read the same wall clock on several steps. The wall
clock registers stay as upstream and return `g_clock`, which now gives every
step of a core its own time. Simulation runs slower in exchange.

Tags keep the `-steps.N` suffix of the earlier `stepping-clock.patch`, which
counted steps in the wall clock instead. Every `-steps.1` release, of
`v1.11.1`, `v1.12.1`, `v1.14.0`, `v1.15.0`, `v1.15.1`, and `v1.16.0`, uses that
stepping clock. `v1.12.1-steps.2`, `v1.15.1-steps.2`, and `v1.16.0-steps.2` use
unit tick, and the `-steps.3` of `v1.12.1`, `v1.15.1`, and `v1.16.0` add the fp32
fix below.
Each release's notes name its patch file and blob.

The Ubuntu 24.04 x86_64 build job runs style and wall-clock behavior checks.
Releases carry no aarch64 libraries, because nothing downstream runs on Arm;
releases before `v1.15.1-steps.3` keep the ones they have.
Pushes and pull requests build the same candidate a release would, the highest
upstream release with its patch, so they test the patch, not `src/` on `main`,
which stays equal to upstream.
Its artifacts supply the release libraries. Publishing creates a draft,
checks asset sizes and hashes, then makes it public automatically. Old-version
backfills do not take the latest label from a newer version.

Releases carry only the Wormhole and Blackhole libraries, `wh` and `bh` with
their multichip variants, because the public source builds only those chips.
Upstream's `libttsim_qsr.so` for Quasar has no counterpart here.

Fork tags are one patch commit on the upstream base. A changed upstream tag is
an error. Tags are never moved and public assets are never overwritten. Drafts
can be resumed after interruption. Matching published source is skipped, while
changed patch output receives the next `steps.N`. No separate state store is
needed. Network operations retry; deterministic failures recur on the next
schedule until the controller or patch is fixed. Actions reports failures.

Regenerate a patch using the command at the top of `release.py`. Versions for
which the patch conflicts are not released; support requires adapting
`unit-tick.patch` to them. Do not suppress simulator error checks to make a release pass.

Lint, type-check, and test the controller from `.github/release`. It is a pixi
workspace with dev tools locked in `pixi.lock`. `scripts/check_style.py` skips
its Python files and lockfile, which follow `ruff format` and pixi instead.

```sh
pixi run ruff format
pixi run ruff check
pixi run pyrefly check
pixi run pytest
```

Run the behavioral check with
`python3 .github/release/test_wall_clock.py /absolute/path/to/libttsim.so`.
The check exercises only simulator libraries, not hardware or model weights.
It loads hand-assembled RV32I programs that read the wall clock twice in a row
and store the difference: one on a busy eth core, and one on BRISC while its
Tensix thread executes a replayed run of `DMANOP`s. Each difference must be 1;
an upstream library gives 0 for the Tensix case, and for the eth case from the
releases with eth busy steps on.

Deployment limits: Actions must be enabled on the fork and the workflow must
be present on its default branch. GitHub may disable scheduled workflows after
60 days of repository inactivity; this workflow cannot recover from its own
disablement. Local regression tests mock GitHub writes; they do not prove
these platform permissions.

The publish job tags and publishes as a GitHub App, because GITHUB_TOKEN can
never get the `workflows` permission and GitHub rejects a tag whose workflow
files differ from the default branch. The App is private to the owner,
installed only on this repository, and grants Contents and Workflows write.
The job reads its ID from the `RELEASE_APP_ID` variable and its key from the
`RELEASE_APP_PRIVATE_KEY` secret, and its token expires with the job.

## Conda package

Each release also carries `ttsim-X.Y.Z+steps.N-0.conda` for `linux-64`,
because conda versions cannot contain `-`. It installs the x86_64 libraries
byte for byte as `lib/ttsim/{wh,bh}/libttsim_<chip>[_xN].so`. Consumers pin it
by URL and sha256, which the publish job prints in its summary. The `package`
job builds it with rattler-build 0.80 through `pixi exec`, and the
publish job uploads and verifies it with the libraries in the same draft.

The package ships no `soc_descriptor.yaml`. tt-metal owns its descriptors, also
reads them on real hardware, and changes them together with its reader code.
UMD reads the descriptor beside the library and fails without it, as tt-metal
0.81 and 0.82 do, so a consumer links its own tt-metal's descriptor into
`lib/ttsim/<chip>/` as `soc_descriptor.yaml`.

rattler-build stamps the build time into the package, so a rebuild of the same
tag has a new sha256. Public assets are never rebuilt, so a pinned hash holds.

## Native fp32 compute configurations

unit-tick.patch also accepts the Wormhole LLK TEN-3868 trailing
UNPACR. Float32-to-Float32 Src writes require Dvalid to remain clear and lose
13 mantissa bits, like TF32. The trailing SrcA zero-fill is accepted only with
these formats and no Dvalid. Readable Float32 Src writes still fail.
An exact zero-fill workaround with no ADC increments is discarded before context
selection because it publishes no data. Other UNPACR instructions select the
instruction context plus the thread context offset; contexts
above 1 remain rejected. The ISA model documents this selection but marks
nonzero Wormhole contexts as weakly specified.

`test_fp32_reload.py` is a workload regression requiring a separate ttnn
runtime. Run it against each candidate with PROBE_FLAGS unset, fp32, and packer.
K spans four blocks so the kernel must reload fp32 partial sums. The test checks
successful completion, output shape, and finite results; it does not establish
bit-exact numerical agreement with silicon.
