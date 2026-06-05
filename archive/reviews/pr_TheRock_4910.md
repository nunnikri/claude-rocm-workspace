# PR Review: TheRock#4910

* **PR:** [ROCm/TheRock#4910](https://github.com/ROCm/TheRock/pull/4910)
* **Reviewed:** 2026-04-21
* **Patterns detected:** Python code, Build system changes, Bash/shell scripts

---

## Summary

This PR changes how sysdeps libraries (`hwloc`, `libpciaccess`) receive the
`rocm_sysdeps_` naming prefix. Previously the rename happened at **install
time** via `patch_linux_so.py --add-prefix rocm_sysdeps_`. Now it happens at
**build time** by patching the upstream `Makefile.in`/`meson.build` files
before the autotools/meson build runs, so the library is born with the correct
name. Install-time logic is simplified to only creating linker symlinks and
fixing `.pc` files.

Additionally, two shared utilities (`update_library_links`, `relativize_pc_file`)
are consolidated from per-package `patch_install.py` copies into
`build_tools/patch_linux_so.py`, and `relativize_pc_file` gains a new
`-L` stripping step.

**Net change:** ~180 lines removed, ~170 added across 7 files.

---

## Overall Assessment

**⚠️ CHANGES REQUESTED** — One blocking error-handling issue and one
CMake correctness concern worth addressing before merge.

**Strengths:**

- Build-time renaming is architecturally cleaner: SONAME is correct from
  the build, no need for post-build `patchelf --set-soname` gymnastics.
- Good DRY refactoring: duplicated `relativize_pc_file` implementations
  (one in `hwloc/patch_install.py`, one in `libpciaccess/patch_install.py`)
  are replaced with a single canonical copy.
- The new `relativize_pc_file` correctly strips leaked build-time `-L` flags
  from `.pc` files, fixing a real issue the old versions missed.
- Linux-only guards are handled correctly in CMakeLists.txt with
  `CMAKE_SYSTEM_NAME STREQUAL "Linux"`.
- New functions have thorough docstrings.

---

## Detailed Review

### ❌ BLOCKING: `update_library_links` uses print+return instead of exceptions

In [`build_tools/patch_linux_so.py`](https://github.com/ROCm/TheRock/blob/main/build_tools/patch_linux_so.py), the new `update_library_links()` function silently degrades on two error paths:

```python
except subprocess.CalledProcessError:
    print(f"Error: No SONAME found in '{libfile}'", flush=True)
    return          # ← silent degradation

if not lib_soname:
    print(f"Error: Empty SONAME for '{libfile}'", flush=True)
    return          # ← silent degradation
```

Per the [Python style guide fail-fast requirements](https://github.com/ROCm/TheRock/blob/main/docs/development/style_guides/python_style_guide.md#fail-fast-behavior):

> `print("ERROR: ..."); return` — Silent degradation. Raise an exception instead.

If patchelf fails or returns an empty SONAME, the linker symlink (`libhwloc.so` /
`libpciaccess.so`) is never created and the install silently produces a broken
library layout. The caller in `patch_install.py` has no way to detect this.

**Required action:** Replace `print(...); return` with `raise RuntimeError(...)`:

```python
except subprocess.CalledProcessError as e:
    raise RuntimeError(f"patchelf --print-soname failed for '{libfile}'") from e

if not lib_soname:
    raise RuntimeError(f"Empty SONAME returned by patchelf for '{libfile}'")
```

---

### ⚠️ IMPORTANT: Double `COMMAND` keyword in CMakeLists.txt (both hwloc and libpciaccess)

Both `hwloc/CMakeLists.txt` and `libpciaccess/CMakeLists.txt` use this pattern:

```cmake
set(patch_source_commands)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  list(APPEND patch_source_commands COMMAND
    bash "${CMAKE_CURRENT_SOURCE_DIR}/patch_source.sh" "${SOURCE_DIR}")
endif()

add_custom_target(
  ...
  COMMAND           # ← outer COMMAND keyword
    ${patch_source_commands}   # ← expands to "COMMAND bash ..." on Linux
  COMMAND
    ...
```

On Linux, `patch_source_commands` already contains the `COMMAND` keyword as its
first element (from the `list(APPEND ... COMMAND bash ...)`). The outer `COMMAND`
produces `COMMAND COMMAND bash ...` — two `COMMAND` keywords in sequence.
CMake interprets this as an empty first command followed by `bash ...`, which
works but is incorrect and fragile.

The correct pattern is to store only the arguments in the variable and rely on
the `COMMAND` keyword in the list being interpreted by CMake's parser directly:

```cmake
# Store COMMAND as part of the variable (no outer COMMAND keyword)
set(patch_source_commands)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  list(APPEND patch_source_commands
    COMMAND bash "${CMAKE_CURRENT_SOURCE_DIR}/patch_source.sh" "${SOURCE_DIR}")
endif()

add_custom_target(
  ...
  ${patch_source_commands}    # ← no outer COMMAND; the one in the variable is used
  COMMAND
    ...
```

**Recommendation:** Remove the outer `COMMAND` keyword before `${patch_source_commands}`
in both `hwloc/CMakeLists.txt` and `libpciaccess/CMakeLists.txt`. This matches
the standard CMake idiom for conditional commands.

---

### ⚠️ IMPORTANT: Inconsistent shebang in `patch_source.sh` scripts

- `hwloc/patch_source.sh`: `#!/usr/bin/bash`
- `libpciaccess/patch_source.sh`: `#!/bin/bash`

`/usr/bin/bash` is non-standard. On Debian/Ubuntu (common CI runners),
`bash` lives at `/bin/bash`. While the script is guarded with
`CMAKE_SYSTEM_NAME STREQUAL "Linux"` and the invocation uses explicit `bash`
(so the shebang only matters if someone runs the script directly), consistency
is still important.

**Recommendation:** Use `#!/usr/bin/env bash` in both scripts — it's portable
across all Linux distributions and is the recommended form in the Bash style guide.

---

### 💡 SUGGESTION: `import re` inside function body

In the new `relativize_pc_file` in `patch_linux_so.py`:

```python
def relativize_pc_file(pc_file: Path) -> None:
    import re
    ...
```

`import re` should be at the module top-level with the other imports. Lazy
imports inside function bodies are usually reserved for cases where the import
is expensive or optional; `re` is stdlib and always available.

---

### 💡 SUGGESTION: Misleading variable name in `patch_install.py` imports

```python
script_path = therock_source_dir / "build_tools" / "patch_linux_so.py"
sys.path.insert(0, str(script_path.parent))
from patch_linux_so import update_library_links, relativize_pc_file
```

`script_path` holds the path to `patch_linux_so.py` (a file), but only
`.parent` (the directory) is actually used. Clearer:

```python
sys.path.insert(0, str(therock_source_dir / "build_tools"))
from patch_linux_so import update_library_links, relativize_pc_file
```

---

### 📋 FUTURE WORK: `get_env_or_exit()` uses `sys.exit()` anti-pattern

Both `patch_install.py` files contain:

```python
def get_env_or_exit(var_name):
    ...
    sys.exit(1)
```

This pre-existed this PR and is not part of the changes, but it's the same
fail-fast violation as the BLOCKING issue above. Worth addressing in a follow-up.

---

## Recommendations

### ❌ REQUIRED (Blocking):

1. In `build_tools/patch_linux_so.py`, replace `print(...); return` with
   `raise RuntimeError(...)` in the two error paths of `update_library_links()`.

### ✅ Recommended:

2. Remove the outer `COMMAND` keyword before `${patch_source_commands}` in
   both `hwloc/CMakeLists.txt` and `libpciaccess/CMakeLists.txt`.
3. Change `#!/usr/bin/bash` to `#!/usr/bin/env bash` in `hwloc/patch_source.sh`
   for consistency with `libpciaccess/patch_source.sh` and portability.

### 💡 Consider:

4. Move `import re` to the module top-level in `patch_linux_so.py`.
5. Simplify the `sys.path.insert` setup in both `patch_install.py` files.

### 📋 Future Follow-up:

6. Replace `sys.exit()` in `get_env_or_exit()` with `raise RuntimeError()` in
   both `patch_install.py` files (pre-existing, out of scope for this PR).

---

## Testing Recommendations

- Verify a Linux build of hwloc produces `librocm_sysdeps_hwloc.so.5` with
  a `libhwloc.so` → `librocm_sysdeps_hwloc.so.5` symlink in the install tree.
- Verify `pkg-config --libs hwloc` returns `-lhwloc` (not `-lrocm_sysdeps_hwloc`)
  from the patched `.pc` file.
- Verify the `libpciaccess.so` symlink is similarly correct.
- Run `readelf -d librocm_sysdeps_hwloc.so.5 | grep SONAME` to confirm SONAME
  matches the filename.
- Confirm the error path in `update_library_links()` raises after the fix.

---

## Conclusion

**Approval Status: ⚠️ CHANGES REQUESTED**

The architectural direction (build-time renaming, shared utilities) is the right
call. The blocking issue is straightforward: two error-path `print+return`
statements in `update_library_links()` need to become `raise RuntimeError(...)`.
The double-COMMAND CMake issue is worth fixing while here since the correct
pattern is a one-line change per file.
