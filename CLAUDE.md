# ROCm Build Infrastructure Project

## Overview

This workspace is for build infrastructure work on ROCm (Radeon Open Compute) via the TheRock repository and related projects.

Project repository: https://github.com/ROCm/TheRock

## Working Environment

**Important:** See `directory-map.md` for all directory locations.

This is a meta-workspace. Actual source and build directories are scattered
across the filesystem and referenced by absolute paths.

This workspace can be active in two environments. Claude detects which one
applies from the current working directory at session start.

### Windows (VSCode / Claude Desktop App)

- Meta-workspace: `C:/Project/Claude-Projects/claude-rocm-workspace`
- TheRock local reference: `C:/Project/Claude-Projects/TheRock-main`
- Scratch directory: `C:/scratch/claude`
- Relative path to edit a file in TheRock: `../TheRock-main/docs/development/README.md`

Windows is used for code review, analysis, and planning. Builds do NOT run
here — all build work happens on the remote Linux machine.

### Remote Linux (nirmal@dell-rack-13)

- Workspace root: `~/Project/Claude-Workspace/`
- TheRock source: `~/Project/Claude-Workspace/TheRock`
- Build tree: `~/Project/Claude-Workspace/therock-build`
- Scratch directory: `~/Project/Claude-Workspace/scratch`
- Scripts: `~/Project/Claude-Workspace/scripts/`

TheRock is cloned with all submodules under the workspace root. All active
work (branches, builds, scratch files) lives under `~/Project/Claude-Workspace/`.

## Project Context

### What is ROCm?

ROCm is AMD's open-source platform for GPU computing. It includes:

- HIP (Heterogeneous-Interface for Portability) - CUDA alternative
- ROCm runtime and drivers
- Math libraries (rocBLAS, rocFFT, etc.)
- Developer tools and compilers

### Build Infrastructure Focus

As a build infra team member, typical work involves:

- CMake build system configuration
- CI/CD pipeline maintenance
- Build dependency management
- Cross-platform build support
- Build performance optimization
- Package generation and distribution
- Package generation include wheel, deb, rpm and tar ball

## Common Tasks

### Building

- Builds typically happen in separate build trees (see directory-map.md)
- Out-of-tree builds are standard practice
- Multiple build configurations (Release, Debug, RelWithDebInfo, asan, tsan) often maintained simultaneously

How we build depends on what kind of task we are doing:

#### Developing Build Infra

Good for making changes to the build infra when we aren't expecting to need to do C++ debugging.

All build commands run on **nirmal@dell-rack-13** under `~/Project/Claude-Workspace/`.

1. CMake configure:

```bash
cmake -B ~/Project/Claude-Workspace/therock-build \
  -S ~/Project/Claude-Workspace/TheRock \
  -GNinja -DTHEROCK_AMDGPU_FAMILIES=gfx1201 \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
```

2. Build entire project (very time consuming)

```bash
cd ~/Project/Claude-Workspace/therock-build && ninja
```

Configuring the project is often tricky. Rely on me to give you task specific instructions for configuration and incremental builds (or else you will initiate very long build time activities).

#### Working on specific components

Often we have to work on specific subsets of ROCm. We do this with -DTHEROCK_ENABLE_* flags as described in TheRock/README.md. Once the project is configured for the proper subset, it is typical to iterate by expunging and rebuilding a specific named project. Example:

```bash
cd ~/Project/Claude-Workspace/therock-build
ninja clr+expunge && ninja clr+dist
```

### Source Navigation

- Source code is across multiple repositories and worktrees
- Git submodules are used extensively
- When editing build configs, check both source tree CMakeLists.txt and build tree caches

### Testing

- Unit tests, integration tests, and packaging tests
- Tests may run on different GPU architectures (gfx906, gfx908, gfx90a, etc.)

## Playbook

Recipes for common multi-step operations.

Scratch directories (large-file-friendly; switch if disk fills up):
- **Windows:** `C:/scratch/claude`
- **Remote Linux:** `~/Project/Claude-Workspace/scratch`

### Download CI artifacts (without extracting)

```bash
# 1. Find the latest successful run for an artifact group
# [Windows] cd /c/Project/Claude-Projects/TheRock-main/build_tools
# [Linux]   cd ~/Project/Claude-Workspace/TheRock/build_tools
python find_latest_artifacts.py --artifact-group gfx110X-all -v

# 2. Download archives to scratch (note the run-id from step 1)
python fetch_artifacts.py \
  --run-id=<RUN_ID> \
  --artifact-group=gfx110X-all \
  --output-dir=<SCRATCH>/artifacts/<RUN_ID> \
  --no-extract
```

Common artifact groups: `gfx110X-all`, `gfx120X-all`, `gfx94X-all`

### Download a subset of CI artifacts and test locally

Useful for validating review feedback (e.g., "would this work if we moved
files around?") without rebuilding. Positional args to `fetch_artifacts.py`
are prefix-matched include filters.

```bash
# 1. Download only the artifacts you need (prefix-match filters)
#    Use --flatten to merge into a single install-prefix-like layout
python fetch_artifacts.py \
  --run-id=<RUN_ID> \
  --artifact-group=gfx110X-all \
  --output-dir=<SCRATCH>/artifacts/<LABEL> \
  --flatten \
  "core-ocl_test" "core-ocl_run" "core-ocl_lib" "base_run" "base_lib"

# 2. Explore the layout
ls <SCRATCH>/artifacts/<LABEL>/bin/
ls <SCRATCH>/artifacts/<LABEL>/tests/

# 3. Rearrange files to test a hypothesis (e.g., "what if tests installed to bin/?")
cp <SCRATCH>/artifacts/<LABEL>/tests/ocltst/* \
   <SCRATCH>/artifacts/<LABEL>/bin/

# 4. Run from the rearranged layout (Linux only — executables are ELF)
cd <SCRATCH>/artifacts/<LABEL>/bin && ./ocltst -m oclruntime.so
```

Notes:
- `--flatten` strips the `<subproject>/stage/` prefix, merging all artifacts
  into a single tree that looks like a standard install prefix (`bin/`, `lib/`,
  `share/`, etc.)
- PR branch artifacts stay in S3 after GH artifact expiry — use the run ID
  from `gh pr checks <URL>` or `gh api repos/.../actions/runs`
- Always test with the ROCm-built libraries, not system-installed ones.
  Comparing against system versions can be useful for diagnosing issues but
  isn't the target test configuration.
- Artifacts built at one driver version may not work on a machine with a
  different driver version — DLL/so resolution may succeed while runtime
  behavior (device enumeration, kernel launch) fails due to driver mismatch

### Inspect an artifact archive without extracting

```bash
# List contents (zstd-compressed)
python -c "
from _therock_utils.artifacts import _open_archive_for_read
from pathlib import Path
with _open_archive_for_read(Path('<archive.tar.zst>')) as tf:
    for m in tf:
        print(m.name)
"
```

## Conventions & Gotchas

### Coding Standards

**Follow the style guides in [TheRock/docs/development/style_guides/](../TheRock/docs/development/style_guides/):**

| Guide | Use For |
|-------|---------|
| [Python Style Guide](../TheRock/docs/development/style_guides/python_style_guide.md) | All Python code |
| [CMake Style Guide](../TheRock/docs/development/style_guides/cmake_style_guide.md) | CMake build configuration |
| [Bash Style Guide](../TheRock/docs/development/style_guides/bash_style_guide.md) | Shell scripts |
| [GitHub Actions Style Guide](../TheRock/docs/development/style_guides/github_actions_style_guide.md) | CI/CD workflows |

Key principles across all languages:

- **Fail-fast**: Never silently continue on errors - raise exceptions immediately
- **Explicit over implicit**: Code should be self-documenting
- **Validate output**: Check that operations actually succeeded
- **DRY/YAGNI/KISS**: Don't repeat yourself, you aren't gonna need it, keep it simple

### Bash Command Conventions

These conventions keep Bash tool calls consistent with the permission rules in `settings.local.json`, reducing unnecessary permission prompts.

- **Paths (Windows)**: Use MSYS2-style paths `/c/Project/Claude-Projects/...` in Bash tool calls (not `C:/Project/...`). Read/Edit/Glob use Windows paths `C:\Project\...` — this only applies to Bash.
- **Paths (Linux)**: Use standard Unix paths `~/Project/Claude-Workspace/...` or absolute `/home/nirmal/Project/Claude-Workspace/...`.
- **Testing**: Use `python -m pytest <path>` (not bare `pytest`, not `python test_file.py`, not `cd <dir> && python -m pytest`). Passing the test path as an argument matches the single `python -m pytest:*` permission rule regardless of which directory the tests are in.
- **Linting**: Use `pre-commit run` (not bare `pre-commit` or `python -m pre_commit`).
- **Prefer separate tool calls over `&&` chains**: Permission matching treats `cmd1 && cmd2` as a single command string, so chained commands may not match individual rules. Use separate Bash tool calls when possible.
- **Copy files into scratch before processing**: Files outside permissioned directories trigger repeated permission prompts. Copy them into the scratch directory first (`C:/scratch/claude/` on Windows, `~/Project/Claude-Workspace/scratch/` on Linux).

### Git Workflow

#### Branch Naming

Use the pattern: `users/<username>/<short-description>`

Examples:

- `users/nunnikri/add-simde-third-party`
- `users/nunnikri/fix-cmake-detection`

#### Creating a Branch and Committing

```bash
# Create and switch to a new branch
# [Linux] cd ~/Project/Claude-Workspace/TheRock
git checkout -b users/nunnikri/<description>

#Run pre-commit
pre-commit run --show-diff-on-failure --color=always --all-files

# Stage changes
git add <files>

# Create commit with structured message and Claude Code footer
git commit -m "$(cat <<'EOF'
<Short summary line>

<Detailed description of what changed and why>

Changes:
- Bullet point list of key changes
- Another change

Additional context or testing notes.

EOF
)"

# Verify commit
git log -1 --stat
```

#### Commit Message Best Practices

- First line: Short summary (50-72 chars)
- Blank line after summary
- Detailed description explaining what and why
- Include "Changes:" section with bullet points for key modifications
- Add testing/verification notes
- Always include the Claude Code footer (emoji + link + Co-Authored-By)
- **DO NOT include issue references** (e.g., "Fixes #123", "Addresses issue #456")
  - Issue tagging happens in pull requests, not individual commits
  - Keeps commit messages focused on what changed, not tracking metadata
- **DO NOT include pull request references** (e.g. "#123")
  - PR tagging happens in pull requests, not individual commits
  - Referencing _commit hashes_ is fine though
- **The "Changes:" section should add context the diff alone doesn't convey**
  - Good: why something was excluded, notable trade-offs, intentional omissions
  - Bad: restating which files were touched or what was added where — that's the diff's job
  - If the summary line already captures the full intent, omit the Changes: section

#### GPG Signing

- **NEVER retry failed commits with `--no-gpg-sign`**
  - The user uses a hardware device (YubiKey, etc.) to sign commits
  - If signing times out, wait for the user to retry manually
  - Do not attempt to bypass GPG signing under any circumstances

#### Submodules

- Git submodules are used extensively
- When editing build configs, check both source tree and build tree caches

#### Running Git in Other Directories

- Use `git -C <path>` instead of `cd <path> && git ...`
- This matches existing Bash permission rules and avoids unnecessary permission prompts
- Example (Linux): `git -C ~/Project/Claude-Workspace/TheRock log --oneline -10`
- Example (Windows): `git -C /c/Project/Claude-Projects/TheRock-main log --oneline -10`

### Review Workflow

Code reviews happen at two levels: **comprehensive reviews** (full PR/branch analysis) and **inline reviews** (quick feedback during iteration).

#### Comprehensive Code Reviews

When you say "review this PR" or "review my branch", Claude performs a comprehensive code review using the system in `reviews/`.

**Triggers** - any of these invoke the review system:

```
Review this PR: https://github.com/ROCm/TheRock/pull/2761
Review PR https://github.com/ROCm/TheRock/pull/2761
Can you review https://github.com/ROCm/TheRock/pull/2761
Review my current branch
Do a style review of my changes
```

**Skills:**

| Command | Description |
|---------|-------------|
| `/review-pr <URL> [types...]` | Review a GitHub PR |
| `/review-branch [types...]` | Review the current local branch |

**Review types** (optional - defaults to comprehensive):
- `style` - Code formatting, naming, conventions
- `tests` - Test coverage and quality
- `documentation` - Docs, comments, help text
- `architecture` - Design, patterns, structure
- `security` - Vulnerabilities, validation, secrets
- `performance` - Efficiency, scaling, resources

**Examples:**

```bash
# Comprehensive review (all aspects)

/review-pr https://github.com/ROCm/TheRock/pull/2761

# Focused reviews
/review-pr https://github.com/ROCm/TheRock/pull/2761 style
/review-branch tests security

# Natural language
Review this PR with focus on architecture: https://github.com/ROCm/TheRock/pull/2761
Do a security review of my branch
```

**Output files:**
- PR reviews: `reviews/pr_{REPO}_{NUMBER}.md` (or `_style.md`, `_tests.md`, etc.)
- Branch reviews: `reviews/local_{COUNTER}_{branch-name}.md`

**Severity levels:**
- `❌ BLOCKING` - Must fix before human review
- `⚠️ IMPORTANT` - Should fix before human review
- `💡 SUGGESTION` - Nice to have
- `📋 FUTURE WORK` - Out of scope for this PR

**Documentation:** See `reviews/README.md` for full details.

#### Inline Reviews (Quick Iteration)

For quick feedback during development, add inline comments with `RVW:` or `RVWY:` markers:

| Marker | Meaning |
|--------|---------|
| `RVW:` | Discuss - Claude proposes fix, waits for confirmation |
| `RVWY:` | YOLO - Claude makes the fix without asking |

```python
# RVW: This logic seems backwards - let's discuss
# RVWY: Add error handling here
```

Then ask Claude to "process review comments" or "fix the RVW comments".

#### Documenting Experiments

When running local experiments (e.g., testing with downloaded CI artifacts),
always include full command + output in the review or notes — not just a
prose summary. Others need to see exactly what was run and what came back.

- Inline the full `$ command` and its output in markdown code blocks
- If logs are too large to inline, save them to a local file (e.g., in
  `/d/scratch/claude/`) so they can be uploaded to a gist for sharing
- Always test with the ROCm-built libraries, not system-installed ones;
  comparing against system versions can be useful for diagnosing issues
  but isn't the target test configuration

### Task Tracking

Track work items in `tasks/active/`.

**Quick reference:**
- Start a task: `/task task-name` or "I'm working on task-name"
- Create new task: Copy `tasks/example-task.md` template
- Complete a task: Move to `tasks/completed/`

### Tools

- `cmake` / `ninja` — build system (Linux only for ROCm builds)
- `ccache` — compiler cache (speeds up incremental builds)
- `clang` / `clang++` — primary compiler for ROCm components
- `clang-format` — C/C++ formatter (enforced by pre-commit)
- `pre-commit` — runs all linters/formatters before commit
- `python3` — build scripts, artifact tools, kpack tooling
- `gh` — GitHub CLI for PR management and artifact queries
- `readelf` / `objcopy` / `objdump` — ELF inspection and manipulation
- `dwz` — DWARF debuginfo compressor (runs during Debian packaging via `dh_dwz`)
- `strip` — symbol stripping (part of packaging pipeline)

## Reference

- [ROCm Documentation](https://rocm.docs.amd.com/)
- [TheRock repository](https://github.com/ROCm/TheRock)

## Remote Machine Setup

### Machine: `nirmal@dell-rack-13`

Layout:

```
~/Project/Claude-Workspace/
├── TheRock/                ← working clone with all submodules (--recursive)
│   ├── rocm-systems/       ← submodule
│   └── rocm-libraries/    ← submodule
├── therock-build/          ← CMake build tree (create as needed)
├── scratch/                ← large temporary files (create as needed)
└── scripts/                ← automation scripts (GitHub poller, mailer, etc.)
```

### Daily sync (cron on nirmal@dell-rack-13)

Syncs TheRock with upstream main every day at 06:00:

```bash
# crontab -e entry:
0 6 * * * cd ~/Project/Claude-Workspace/TheRock && \
  git pull --ff-only >> ~/Project/Claude-Workspace/TheRock/sync.log 2>&1 && \
  git submodule update --recursive >> ~/Project/Claude-Workspace/TheRock/sync.log 2>&1 && \
  echo "$(date): sync complete" >> ~/Project/Claude-Workspace/TheRock/sync.log
```

Check status: `tail -20 ~/Project/Claude-Workspace/TheRock/sync.log`

### Safety restrictions

Unless explicitly asked, Claude must NOT on the remote machine:

- `git push` any branch
- Delete files (`rm -rf` or bulk deletion)
- Install packages (`pip install`, `apt install`, `npm install`, etc.)
- Execute untrusted binaries from the repository
- Run build systems (`cmake --build`, `ninja`, `make`) — exception: static analysis only
- Run arbitrary scripts from the repo (`./scripts/foo.sh` etc.)
- Execute containers (`docker run`, etc.)
- Modify infrastructure (Terraform, cloud CLI write ops)
- Access or print external secrets
- Perform network fuzzing or active exploitation
- Open pull requests automatically (`gh pr create`, `gh pr merge`)

## Notes

- Note that TheRock is a super-project. The builds under the submodules (like rocm-systems) are sub-projects. Since dependency management is handled by the super-project, you want to refer to those build rules. For example, in the case of ROCR-Runtime and clr, see the `core/CMakeLists.txt` file. This is documented in docs/development/build_system.md.
- Never do `git push` without explicit authorization.
- Do not amend commits without explicit authorization. Stage changes and ask for reviews before commiting.
- Don't be a sycophant and stroke my ego about how right I am when I make suggestions. Remember that I can be wrong too and feel free to engage in light debate if my reasoning seems unsound but accept when I make a decision.
- Don't claim that the result of work is "production" code or use shaky metrics to justify how things are progressing. Just say how things are without superlatives.
- Before committing to rocm-kpack, run pre-commit.
- When writing design docs, always include an "Alternatives Considered" section to list major, rejected options. Don't include nit-picky differences, just major architectural alternatives.
