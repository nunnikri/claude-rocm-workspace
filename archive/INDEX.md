# Archive Knowledge Base

This document synthesizes patterns, invariants, and lessons extracted from all
archived files in this workspace (reviews, tasks, reports, plans, prototypes).
Use it as a first-stop reference when starting a new review or task — look up
the relevant topic, read the watch-outs and conditions, then follow the File
Index links for depth. The goal is "when doing X, check Y" not a file listing.

---

## Learnings by Topic

---

### 1. CI / Workflow Changes (GitHub Actions)

**Conditions / Invariants**

- `benc-uk/workflow-dispatch` inputs are invisible to actionlint — it sees only
  a string `inputs` parameter, not the JSON content. The only validation is a
  runtime test (`build_tools/github_actions/tests/workflow_dispatch_inputs_test.py`).
- `workflow_call` inputs do NOT propagate across boundaries automatically. Each
  reusable workflow's `inputs.*` context contains only its own declared inputs,
  not the dispatcher's inputs.
- IAM guard patterns evolved: `github.repository == 'ROCm/TheRock'` is brittle
  (breaks forks); `inputs.iam_role != ''` is the preferred gate. An empty string
  is falsy; the string `"None"` is truthy — see the `gha_set_output` gotcha below.
- Concurrency group `${{ github.workflow }}-${{ github.event.number || github.sha }}`
  means multiple `workflow_dispatch` runs on the same branch (same SHA) cancel
  each other. Use `${{ github.head_ref || github.run_id }}` instead.
- PyYAML parses the YAML key `on:` as Python `True` (YAML 1.1 boolean), not the
  string `"on"`. All workflow analysis code must use `workflow.get(True, {})` not
  `workflow.get("on", {})`.

**Recurring Issues**

- `gha_set_output` calls `str()` on all values: `None` → `"None"` (non-empty
  string). Anything consuming the output with `if: inputs.foo != ''` will
  incorrectly evaluate as truthy. Always coerce `None` to `""` before passing:
  `config.write_access_iam_role or ""`.
- `benc-uk/workflow-dispatch` bug class: passing an input that the target
  workflow's `on: workflow_dispatch: inputs:` section doesn't define → GitHub
  API returns "Unexpected inputs provided: [...]" at runtime.
- Double-trigger on PRs: `labeled` and `synchronize` events fire simultaneously,
  share the same `event.number`, and one cancels the other. Fix: switch to
  `head_ref`-based concurrency groups.

**Resolutions**

- Dynamic test generation in `workflow_dispatch_inputs_test.py`: one pytest test
  per workflow file that contains dispatch calls. Validates both unexpected inputs
  and missing required inputs.
- Enforce workflow filenames (not IDs or display names) in the `workflow` field —
  enables local file resolution for validation.

**Watch-outs**

- Stage skip conditions must use `if: ${{ !cancelled() && !failure() }}` — not
  just `if: success()` — or a prebuilt stage (which produces a "skipped" result)
  will cascade and skip all downstream stages.
- When adding a new input to a reusable workflow called via
  `benc-uk/workflow-dispatch`, also add it to every dispatch callsite's `inputs:`
  JSON, or get a runtime failure in the next CI run.

---

### 2. Multi-arch Builds (prebuilt, configure, tarball, release)

**Conditions / Invariants**

- Multi-arch call chain: `multi_arch_ci.yml` → `setup_multi_arch.yml` →
  `multi_arch_ci_linux.yml` → `multi_arch_build_portable_linux.yml` →
  `multi_arch_build_portable_linux_artifacts.yml`. Release call chain forks at
  the orchestrator: `release_multi_arch.yml` → `setup_multi_arch.yml` +
  `multi_arch_build_portable_linux.yml` directly (skips `multi_arch_ci_linux.yml`).
- `configure_multi_arch_ci.py` implements a 6-step pure transformation pipeline:
  `CIInputs.from_environ()` → skip check → target selection → stage decisions →
  matrix entry construction → `CIOutputs`. All steps are pure functions; only the
  last step writes to the environment.
- `CIInputs.from_environ()` reads `GITHUB_EVENT_PATH` directly (not individual
  env vars) — eliminates 10+ env var pass-throughs but requires a contract test
  to verify field names.
- Per-stage prebuilt: S3 server-side copy (boto3 `s3_client.copy()` = CopyObject
  API). 185 artifacts + 185 sha256sums copied in ~16 seconds.
- **Multi-arch release PRs must update the matrix in ALL three workflow files:
  `multi_arch_ci.yml`, `multi_arch_ci_linux.yml`, and `release_multi_arch.yml`.
  Missing one produces a silent mismatch.**

**Performance Baselines**

- ccache hit rate dominates wall time. Warm families: 15-42 min; cold-cache
  families: 125-173 min. RCCL linking for `comm-libs` is the known bottleneck
  for gfx1153 (~172 min).
- Windows ccache: `actions/cache` was ~57% warm / 0% cold due to 4 GB limit
  perpetually full. Bazel-remote backend achieves 50.5% remote hit rate with
  24-39% build time reduction.

**Watch-outs**

- Prebuilt artifacts are only safe to reuse for `pull_request` triggers. Version
  embedding (git-tag-derived version strings) makes reuse risky for `push` or
  `schedule` triggers because the version would be stale.
- Windows stages: only foundation, compiler-runtime, and math-libs. comm-libs,
  dctools-core, profiler-apps, media-libs are disabled on Windows. Any code that
  iterates stage lists must filter by platform.
- Tarball compression: `tar cfz` is the current default. `zst-3` is 14% smaller
  and 5× faster to compress but is a format change requiring downstream tooling
  updates.

---

### 3. Packaging (deb, wheel, rpm, tarball, artifact layout)

**Conditions / Invariants**

- Component inheritance chain: `lib → run → dbg → dev → doc` (each `extends`
  the previous). `test` is standalone with no `extends`.
- A `run` descriptor with empty `includes` is a catch-all: it claims everything
  not already claimed by `lib`. Intentional for some projects but a bug in others
  (miopen, rocprofiler-sdk, base, rocrtst, aqlprofile).
- `include` lists AUGMENT defaults, they do NOT replace them. To restrict a
  component's file set, use `exclude` patterns — not narrow `include` lists.
- S3 layout: `{run_id}-{platform}/{name}_{component}_{family}.tar.xz` at root;
  `logs/{artifact_group}/` and `manifests/{artifact_group}/` in subdirectories;
  `python/{artifact_group}/` for Python wheels.
- `RunOutputRoot` is the single source of truth for path computation.
  `OutputLocation` objects carry `.s3_uri`, `.https_url`, and `.local_path()`.

**Recurring Issues**

- `test extends doc` change shifted file ownership: previously `test`
  independently re-claimed files; after the change `run` wins first and test gets
  nothing.
- `dev` defaults can steal from `test`: rocgdb (`tests/*/include/`) and
  hipSPARSE (`share/hipsparse/test/*.cmake`) are examples.
- Flat pip index vs PEP-503 index: S3 doesn't serve `index.html` for directory
  requests, so `--index-url` (PEP-503) fails. Use `--find-links` (flat index).

**Watch-outs**

- `Path.resolve()` on Windows with Git Bash paths can produce incorrect results.
  Don't use it in cross-platform scripts.
- Multiple parallel uploads to the same `python/` directory create race conditions
  on `index.html`. Server-side index generation (AWS Lambda) is the planned fix.
- Release workflows: the CI bucket (`therock-ci-artifacts`) and release buckets
  (`therock-{dev,nightly,prerelease}-artifacts`) are intentionally separate trust
  boundaries. Never upload CI artifacts to release buckets.

---

### 4. Python Build Tools (style, correctness, test coverage)

**Conditions / Invariants**

- 84% of production Python files have no tests (as of 2026-01-09 audit). Critical
  untested: `build_package.py` (768 LOC), `buildctl.py` (328 LOC),
  `install_rocm_from_artifacts.py` (403 LOC), `configure_stage.py`.
- `configure_multi_arch_ci.py` is the gold standard: 6 pure transformation steps,
  90% test coverage, 43 tests, explicit dataclasses for inputs/outputs, reads
  `GITHUB_EVENT_PATH` directly.
- `os.environ.get` preferred over `os.getenv` — `os.environ` gives both `.get()`
  (optional) and `[]` (required) from one interface.

**Recurring Issues**

- `label.split(":")` without `maxsplit` crashes on labels with multiple colons.
  Use `label.split(":", maxsplit=1)`.
- Broad `except Exception` silences real errors. Catch specific exceptions.
- Tuple returns instead of `@dataclass` makes return values opaque and hard to test.
- Live tests (hitting real GitHub API or S3) beat mocked credential tests. Mocks
  don't test real behavior — as demonstrated by the S3 auth bug where mocks
  passed but CI failed due to `AWS_SHARED_CREDENTIALS_FILE` not being respected.

**Watch-outs**

- Scripts that read `GITHUB_EVENT_PATH` directly require a contract test
  (regex-extract field names from workflow YAML and assert against dataclass
  fields). Without it, workflow/script field name drift is invisible until CI.
- `assert` for input validation is disabled with `-O`. Use explicit `raise
  ValueError(...)` instead.
- Benchmarking infrastructure (~2700 LOC in build_tools) has zero test coverage
  as of 2026-01-09 audit.

---

### 5. CMake Patterns (FetchContent, external URLs, subproject deps)

**Conditions / Invariants**

- ~74 `PATHS /opt/rocm` sites in find_package/find_program calls across
  rocm-systems and rocm-libraries. These cause sandbox escapes when a host ROCm
  install exists.
- `CMAKE_INSTALL_PREFIX FORCE` in ~10 projects (rocblas, hipblaslt, hipsparselt,
  etc.) prevents TheRock from controlling the install prefix. The `FORCE` keyword
  must be removed.
- `FETCHCONTENT_SOURCE_DIR_<name>` CMake variable redirects a FetchContent fetch
  to a local directory without patching submodule code. This is the preferred fix
  for subprojects that fetch things the super-project should provide.
- ~14 subprojects fetch rocm-cmake from GitHub when the super-project should
  provide it via `find_package(rocm-cmake)`.

**Recurring Issues**

- P0 external URL: `perftools.pages.jsc.fz-juelich.de` (otf2) is an active
  build-breaking external dependency not on the S3 mirror.
- In-function `set(VAR ... PARENT_SCOPE)` guards fail at 2+ function call depth —
  `PARENT_SCOPE` only reaches one level up. Fix: use `file(COPY)` at file scope.
- Silent fallback to hardcoded `/opt/rocm` paths should fail loudly
  (`message(FATAL_ERROR ...)`) instead.

**Watch-outs**

- When splitting a subproject's artifacts, update ALL of: BUILD_TOPOLOGY.toml
  (deps + ordering), CMakeLists.txt add_subdirectory order, artifact .toml
  descriptors (stage paths), and the compiler-runtime stage artifact_group
  ordering.
- Reject upstream PRs that add new `PATHS /opt/rocm` hints — use
  `CMAKE_PREFIX_PATH` instead.
- `MLIR tablegen` dependency hacks in out-of-tree builds are fragile — if
  MLIR/Flang internal target names change, these fail silently.

---

### 6. Windows CI / Cross-platform (file locking, process hangs, DLL issues)

**Conditions / Invariants**

- Windows build directory must be `B:\build` (a separate drive letter) to avoid
  MAX_PATH issues during nested builds.
- `RuntimeTearDown` in HIP/CLR is compiled out on Windows. On Linux it drains
  pending GPU streams; on Windows, no equivalent runs.
- `AMD_DIRECT_DISPATCH` defaults to false on Windows (worker thread mode) and
  true on Linux (direct dispatch). The `HostQueue::finish()` hang only occurs in
  worker thread mode.
- Strawberry Perl PATH entry must use `$PATH;` prefix — if it appears before the
  system PATH, Strawberry's cmake shadows the system cmake.

**Recurring Issues**

- PyTorch atexit hang: `__hipUnregisterFatBinary` → `SyncAllStreams(true)` →
  `HostQueue::finish()` → `awaitCompletion()` on a dead worker thread hangs
  forever.
- `WinError 32` (file locking) during artifact extraction: bare `os.unlink()`
  calls in `artifacts.py` and `pattern_match.py`. Windows Defender/Search Indexer
  can transiently lock files. `_rmtree_with_retry()` exists as a precedent.
- `DLLS_COPIED` guard bug: parallel POST_BUILD commands race when writing the same
  DLLs to the same output directory. Affects rocprim (66 targets) and hipcub
  (48 targets).

**Resolutions**

- PyTorch hang fix: `torch.cuda.synchronize()` before exit drains pending GPU
  work. `os.kill(os.getpid(), signal.SIGTERM)` also works. `os._exit()` does NOT
  bypass the hang.
- CLR fix: early return in `HostQueue::finish()` when
  `!AMD_DIRECT_DISPATCH && !Os::isThreadAlive(thread_)`.
- Heisenbug note: attaching a debugger gives GPU ops time to complete, making the
  hang disappear. Don't conclude it's fixed based on debugger-attached test runs.

**Watch-outs**

- `build_prod_wheels.py` build step on Windows requires cmd shell (not bash) —
  this is load-bearing. Don't switch the build step shell for Windows pytorch
  workflow.
- PyTorch Windows testing: only works for torch 2.9 due to untriaged failures on
  2.10. CI build uses 2.10 to catch build breakage; testing on 2.10 is future work.
- Windows CI currently tests only 3 stages. Any stage list code that doesn't
  filter by platform will include Linux-only stages.

---

### 7. Code Review Hygiene (PR patterns)

**Conditions / Invariants**

- Blocking hygiene issues found repeatedly: PR title starts lowercase, description
  says "what" not "why", silent path changes not explained, no CI evidence for
  build/test changes.
- Every PR touching a reusable workflow should include a caller inventory in the
  description (which workflows call the changed one, what was passed to them).
- PRs that touch artifact descriptors must verify file ownership doesn't overlap —
  run the artifact builder and check for duplicates.

**Recurring Issues**

- Tests removed during migration (over-deletion): when migrating to a new API,
  removing test cases that exercised non-migrated behavior. Diff should only touch
  constructor calls, not remove test coverage.
- CI failures in PR checks that are "pre-existing/unrelated" must still be
  explicitly acknowledged so reviewers don't spend time debugging them.

**Review severity taxonomy:**

- `❌ BLOCKING` — Must fix before human review
- `⚠️ IMPORTANT` — Should fix before human review
- `💡 SUGGESTION` — Nice to have
- `📋 FUTURE WORK` — Out of scope for this PR

**Watch-outs**

- Artifact descriptor changes that look mechanical (path rename, stage move) can
  silently break file ownership. Always verify with the artifact builder.
- The "Alternatives Considered" section is required in design docs and PRs: list
  major architectural alternatives with reasons for rejection.
- Don't approve based on CI passing alone when there are heisenbug conditions
  (Windows GPU hang) or environment-dependent failures.

---

### 8. S3 / Artifact Infrastructure (auth, IAM, naming, backends)

**Conditions / Invariants**

- Bucket naming convention:
  - `therock-ci-artifacts` — main repo CI
  - `therock-ci-artifacts-external` — fork CI (PRs from forks)
  - `therock-{dev,nightly,prerelease}-artifacts` — release build outputs
  - `therock-{dev,nightly,prerelease}-tarball` — tarball publish
  - `therock-{dev,nightly,prerelease}-python` — Python wheel publish
- `boto3.Session().get_credentials()` respects the full credential chain
  including `AWS_SHARED_CREDENTIALS_FILE`. Manual `os.environ.get("AWS_ACCESS_KEY_ID")`
  checks do NOT — they bypass the credentials file used by CI runners.
- Read backends use UNSIGNED fallback for public buckets. Write backends fail-fast
  on missing credentials. This asymmetry is intentional.

**Recurring Issues**

- `gha_set_output` bug: `str(None)` → `"None"` (truthy string). IAM role lookup
  set `"None"` as the role ARN for fork PRs. Fix: `config.write_access_iam_role or ""`.
- Parallel boto3 uploads: sequential `upload_file()` calls take 10× longer than
  `aws s3 cp --recursive`. Fix: `ThreadPoolExecutor(10)` in
  `S3StorageBackend.upload_files()`.
- S3 `SignatureDoesNotMatch` error on Windows CI: usually caused by stale
  credentials from a K8s cluster restart, not a code bug. Retrigger the job.

**Watch-outs**

- Old IAM pattern `github.repository == 'ROCm/TheRock'` for fork detection is
  fragile. Use `inputs.iam_role != ''` as the gate.
- S3 retention: artifacts older than the retention window are deleted. Tests that
  pin specific commit SHAs and run IDs will fail when those artifacts expire.
- Two-tier S3 access: internal CI bucket is private; external bucket is public
  with UNSIGNED read access. Download code that assumes one or the other will
  break for the opposite case.

---

### 9. Security (from audits)

**Conditions / Invariants**

- ~74 `PATHS /opt/rocm` sites in CMake files create sandbox escapes when a host
  ROCm install exists.
- `CMAKE_INSTALL_PREFIX FORCE` in ~10 subprojects allows the subproject to
  override TheRock's install prefix. If a developer has a stale CMakeCache,
  builds can install to `/opt/rocm` instead of the stage directory.
- 92 `#if HIP_VERSION` guards in the codebase are a permanent risk surface for
  version bumps — each is a latent correctness bug waiting for the next major HIP
  version change.

**Recurring Issues**

- CMake configure mutates the source tree: `file(WRITE ${HIP_COMMON_DIR}/VERSION ...)`
  overwrites the `VERSION` file in the source tree (not just the build tree).
- Tag pushed after merge: CI on the merge commit cannot see the new version tag.
  Windows never gets a real version: git-tag lookup is `#if UNIX`.
- Silent fallback paths (hardcoded `/opt/rocm`) used without logging. A developer
  in a clean environment wouldn't notice until a build breaks elsewhere.

**Resolutions**

- REC-1: Remove silent `/opt/rocm` fallbacks — fail loudly with
  `message(FATAL_ERROR ...)`.
- REC-2: Remove `FORCE` from `CMAKE_INSTALL_PREFIX` set statements.
- REC-3: Remove `PATHS /opt/rocm/...` from `find_package` calls — rely on
  `CMAKE_PREFIX_PATH` set by the super-project.
- PR #3825 (redirect ROCM_PATH to build tree) was rejected: team consensus is to
  fix subprojects individually, not paper over at the super-project level.

**Watch-outs**

- Reject upstream PRs that add new `PATHS /opt/rocm` hints.
- The `hip-config-amd.cmake.in` `PATHS "/opt/rocm"` in `find_dependency` calls
  is the highest-impact install-time path bug — causes broken HIP_PLATFORM
  detection when packages are installed side-by-side.
- Version tag timing: if CI validates the merge commit's version before the
  release tag is pushed, it will see a wrong version.

---

## File Index

| Topic | File | One-line summary |
|-------|------|-----------------|
| CI / Workflow | `tasks/completed/github_actions_static_analysis.md` | Dynamic pytest for `benc-uk/workflow-dispatch` input validation |
| CI / Workflow | `tasks/active/concurrency-groups.md` | Fix `workflow_dispatch` mutual cancellation via `head_ref`-based concurrency groups |
| CI / Workflow | `tasks/active/configure-ci-refactor.md` | Style/coverage analysis of `configure_ci.py`: mutable defaults, 295-line function, zero coverage on `main()` |
| CI / Workflow | `tasks/completed/workflow-summary.md` | CI workflow summary format design and iteration |
| Multi-arch | `tasks/completed/multi-arch-configure.md` | `configure_multi_arch_ci.py`: 6-step pure pipeline, `BuildConfig` contract test |
| Multi-arch | `tasks/completed/multi-arch-migration.md` | Gap analysis vs standard CI; Windows ccache gap; `post_stage_upload.py` design |
| Multi-arch | `tasks/active/multi-arch-releases.md` | Release workflow architecture: bucket naming, IAM role selection |
| Multi-arch | `tasks/active/multi-arch-prebuilt.md` | Per-stage prebuilt design: S3 server-side copy, 185 artifacts in ~16s |
| Multi-arch | `tasks/completed/multi-arch-windows-ci.md` | Windows 3-stage pipeline: platform filter bug, compiler path quoting, Strawberry PATH fix |
| Multi-arch | `tasks/active/test-workflow-overhead.md` | Windows setup overhead: choco/pip/dvc per-stage cost breakdown |
| Packaging | `tasks/completed/artifact-overlap.md` | Component inheritance chain, `run` catch-all pattern, `include` augments defaults |
| Packaging | `tasks/completed/python-packages-ci.md` | `piprepo` → `indexer.py`; `--find-links` vs `--index-url`; CI orchestration |
| Packaging | `tasks/active/pytorch-ci.md` | PyTorch CI architecture: `build_prod_wheels.py`, `build_pytorch` positive selection |
| Packaging | `reviews/pr_TheRock_4308.md` | Kpack-split packaging: arch-neutral + per-ISA device wheels |
| Packaging | `tasks/active/run-outputs-layout.md` | `RunOutputRoot` + `OutputLocation` design; 10× upload regression from sequential boto3 calls |
| Python Tools | `reports/python_audit_build_tools_2026-01-09.md` | 84% of production files untested; critical scripts listed; style violations enumerated |
| Python Tools | `tasks/active/artifacts-for-commit.md` | `find_artifacts_for_commit.py`: `GitHubAPI` class, `ArtifactRunInfo` dataclass, rate limit handling |
| Python Tools | `tasks/active/measure-expand-python-test-coverage.md` | Test coverage expansion plan for build_tools |
| CMake | `reports/audit_fetchcontent_external_urls.md` | ~70 external FetchContent calls; P0 otf2 URL; `FETCHCONTENT_SOURCE_DIR` redirect strategy |
| CMake | `reports/path-audit/recommendations.md` | REC-1..4: silent fallbacks, FORCE override, PATHS hints, standalone helpers |
| CMake | `tasks/active/dlls-copied-fix.md` | `DLLS_COPIED` guard fails at 2+ function depth; fix: `file(COPY)` at file scope |
| CMake | `reviews/pr_TheRock_3928.md` | amd-llvm split: BUILD_TOPOLOGY deps, Fortran error message bug, MLIR tablegen fragility |
| CMake | `reviews/pr_TheRock_4235.md` | `THEROCK_DEV_PROJECTS`: sentinel-after-stamp forces inner build; `IN_LIST` idiom |
| CMake | `tasks/active/third-party-amd-llvm-toolchain.md` | amd-llvm toolchain split into base/compiler-runtime/flang/offload stages |
| Windows CI | `tasks/active/windows-process-hang.md` | PyTorch atexit hang: `SyncAllStreams` on dead worker thread; fix: `synchronize()` or SIGTERM |
| Windows CI | `tasks/active/clr-finish-hang-test.md` | CLR PR #3790: early return in `HostQueue::finish()` when thread is dead |
| Windows CI | `tasks/active/windows-file-locking.md` | `WinError 32` from bare `os.unlink()`; `_rmtree_with_retry()` precedent |
| Code Review | `reviews/pr_2939_hygiene.md` | Hygiene failures: lowercase title, "what not why" description, unexplained path change |
| Code Review | `reviews/pr_TheRock_4161.md` | Bazel-remote ccache: CI evidence table; 50.5% remote hit rate |
| Code Review | `tasks/completed/skill-definitions-for-reviews.md` | Review skill definitions and severity taxonomy |
| S3 / Artifacts | `reviews/local_026_users-scotttodd-s3-iam-lookup.md` | `gha_set_output(None)` → `"None"` → IAM role assumption failure for fork PRs |
| S3 / Artifacts | `reviews/local_022_users-scotttodd-s3-auth-simplification-2.md` | `boto3.Session().get_credentials()` vs manual env check; live tests beat mocked tests |
| Security | `reports/audit_hip_version_automation.md` | Windows never gets real version; CMake mutates source tree; `ERROR_QUIET` swallows failures |
| Security | `plans/path_resolution_plan.md` | Two-bucket fix approach; linter script rules; env-var redirect approach rejected |
