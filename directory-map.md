# ROCm Directory Map

This document maps all ROCm-related directories for both environments.
Claude detects the active environment from the working directory at session start.

---

## Windows (VSCode / Claude Desktop App)

| Alias | Path | Notes |
|-------|------|-------|
| `workspace` | `C:/Project/Claude-Projects/claude-rocm-workspace` | This meta-workspace |
| `therock` | `C:/Project/Claude-Projects/TheRock-main` | Local reference clone (read-only analysis) |
| `scratch` | `C:/scratch/claude` | Large temporary files |

Builds do NOT run on Windows. This environment is for code review, analysis, and planning only.

---

## Remote Linux (amd@dell-rack-13)

### Reference (read-only, daily sync)

| Alias | Path | Notes |
|-------|------|-------|
| `ref-therock` | `~/Nirmal/Claude-workspace/ref-code/TheRock` | Upstream clone with all submodules |
| `ref-rocm-systems` | `~/Nirmal/Claude-workspace/ref-code/TheRock/rocm-systems` | Submodule |
| `ref-rocm-libraries` | `~/Nirmal/Claude-workspace/ref-code/TheRock/rocm-libraries` | Submodule |
| `third-party` | `~/Nirmal/Claude-workspace/ref-code/third-party` | Extracted third-party sources |

### Active work

| Alias | Path | Notes |
|-------|------|-------|
| `therock` | `~/Nirmal/Claude-workspace/workspace/TheRock` | Working clone (feature branches here) |
| `build` | `~/Nirmal/Claude-workspace/workspace/therock-build` | CMake build tree |
| `scratch` | `~/Nirmal/Claude-workspace/workspace/scratch` | Large temporary files |

---

## Third-party sources (ref-code/third-party)

Downloaded once from `rocm-third-party-deps.s3.us-east-2.amazonaws.com`.
See `ref-code/fetch_third_party.sh` to re-download or add packages.

| Directory | Version |
|-----------|---------|
| `boost-1.87.0/` | 1.87.0 |
| `Catch2-3.8.1/` | 3.8.1 |
| `eigen-3.4.0/` | 3.4.0 |
| `elfio-3.12/` | 3.12 |
| `fftw-3.3.10/` | 3.3.10 |
| `flatbuffers-25.9.23/` | 25.9.23 |
| `fmt-11.1.3/` | 11.1.3 |
| `frugally-deep-0.15.31/` | 0.15.31 |
| `FunctionalPlus-0.2.25/` | 0.2.25 |
| `googletest-1.16.0/` | 1.16.0 |
| `grpc-v1.78.1/` | 1.78.1 |
| `OpenBLAS-18638c7/` | 18638c7 |
| `libdivide-5.2.0/` | 5.2.0 |
| `msgpack-cxx-7.0.0/` | 7.0.0 |
| `nlohmann-json-3.12.0/` | 3.12.0 |
| `openmpi-5.0.9/` | 5.0.9 |
| `simde-0.8.2/` | 0.8.2 |
| `spdlog-1.15.3/` | 1.15.3 |
| `SuiteSparse-7.8.3/` | 7.8.3 |
