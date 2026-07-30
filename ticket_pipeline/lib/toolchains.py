"""
toolchains - per-language/build-system defaults for pipeline_lib.py.

Detects which toolchain a project uses (by marker file at the project
root) and supplies that toolchain's default build/test/lint commands and
the security-sensitive bits pipeline_lib needs to gather acceptance-
criteria evidence safely from ticket-derived text (see
pipeline_lib.extract_plan_commands): which single binary is trusted to
run, and which of its subcommands are safe (read-only, no side effects
beyond compiling/running tests).

Detection order matters when a repo has more than one marker file (e.g.
a Cargo workspace vendored inside a Bazel monorepo) - the first match in
TOOLCHAINS wins. For anything that detection gets wrong, a project-local
.dev-pipeline.toml (see pipeline_lib.load_pipeline_config) overrides
individual command keys regardless of which toolchain was detected, so
detection only needs to get the *common* case right.

Defaults for less-common gates (format/lint) are best-effort placeholders
for toolchains where there's no single dominant tool (e.g. C++ lint
depends heavily on project conventions) - check the printed
"using <toolchain> defaults" message against your project's real tooling
and override via .dev-pipeline.toml if they don't match.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Toolchain:
    name: str
    # Any one of these present at the project root is enough to detect
    # this toolchain.
    marker_files: tuple[str, ...]
    # Same shape/keys as pipeline_lib.DEFAULT_COMMANDS - build_cmd,
    # test_compile_cmd, test_cmd, test_filter_cmd, fmt_fix_cmd,
    # clippy_fix_cmd, fmt_check_cmd, clippy_cmd. Names are inherited from
    # the original Rust-only defaults (fmt/clippy) but are generic format-
    # fix/lint-fix/format-check/lint-check slots regardless of language -
    # not worth a breaking rename for what's just a label.
    commands: dict[str, str]
    # The single binary name extract_plan_commands trusts in ticket-
    # derived text (e.g. "cargo", "bazel", "ctest", "npm"). Never widen
    # this without re-reading extract_plan_commands' threat model comment
    # in pipeline_lib.py - ticket content is untrusted external input.
    evidence_binary: str
    # Which subcommands of evidence_binary are safe to run as
    # acceptance-criteria evidence (read-only test runs, not
    # build/fmt/lint side effects). None means "no subcommand gate" - for
    # tools like ctest where the binary itself only ever runs tests, so
    # there's nothing further to restrict.
    evidence_subcommands: frozenset[str] | None
    # Regex (as a string, compiled by pipeline_lib) matching the
    # evidence-bearing lines in this toolchain's test output, so a tail
    # truncation doesn't accidentally drop the pass/fail summary when
    # it's not at the very end of the output.
    test_output_signal_pattern: str


RUST = Toolchain(
    name="rust (cargo)",
    marker_files=("Cargo.toml",),
    commands={
        "build_cmd": "cargo build",
        "test_compile_cmd": "cargo test --no-run",
        "test_cmd": "cargo test",
        "test_filter_cmd": "cargo test {filter}",
        "fmt_fix_cmd": "cargo fmt",
        "clippy_fix_cmd": "cargo clippy --fix --allow-dirty --allow-staged --allow-no-vcs",
        "fmt_check_cmd": "cargo fmt -- --check",
        "clippy_cmd": "cargo clippy -- -D warnings",
    },
    evidence_binary="cargo",
    evidence_subcommands=frozenset({"test"}),
    test_output_signal_pattern=r"FAILED|^test result:|panicked at|^---- ",
)

# Bazel takes priority over CMake/Cargo when both a Bazel marker and
# something else are present, since Bazel monorepos commonly vendor or
# wrap other build systems' projects as targets - the outer Bazel build
# is almost always the one you actually want to drive.
BAZEL = Toolchain(
    name="bazel",
    marker_files=("WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel"),
    commands={
        "build_cmd": "bazel build //...",
        "test_compile_cmd": "bazel build //...",
        "test_cmd": "bazel test //...",
        "test_filter_cmd": "bazel test {filter}",
        "fmt_fix_cmd": "buildifier -r .",
        "clippy_fix_cmd": "true",
        "fmt_check_cmd": "buildifier -r --lint=warn -mode=check .",
        "clippy_cmd": "true",
    },
    evidence_binary="bazel",
    evidence_subcommands=frozenset({"test"}),
    test_output_signal_pattern=r"FAILED|PASSED|^//.*\s+(PASSED|FAILED)|Executed \d+ out of",
)

CMAKE = Toolchain(
    name="cmake/ctest",
    marker_files=("CMakeLists.txt",),
    commands={
        "build_cmd": "cmake --build build",
        "test_compile_cmd": "cmake --build build",
        "test_cmd": "ctest --test-dir build",
        "test_filter_cmd": "ctest --test-dir build -R {filter}",
        "fmt_fix_cmd": "clang-format -i",
        "clippy_fix_cmd": "clang-tidy --fix",
        "fmt_check_cmd": "clang-format --dry-run --Werror",
        "clippy_cmd": "clang-tidy",
    },
    evidence_binary="ctest",
    # ctest has no subcommand to gate on - the whole binary only ever
    # runs tests, so any flags after it are fine.
    evidence_subcommands=None,
    test_output_signal_pattern=r"Failed|Passed|tests passed|% tests passed|FAILED",
)

# Python projects using pytest. Python has no separate compile step,
# so test_compile_cmd uses pytest's --collect-only (imports all test
# modules and collects test items without running them - catches
# syntax/import/collection errors, the closest analog to `cargo test
# --no-run` for a dynamic language). build_cmd reuses the same command:
# for a manual-verification criterion, "does the project's test suite
# still collect cleanly" is the most useful gate available without a
# real build step. Ruff covers both formatting and linting; projects
# using black + flake8 (or others) override via .dev-pipeline.toml.
# Placed after CMake but before SvelteKit/TypeScript: a pure Python
# project won't have Cargo.toml/CMakeLists.txt, so it won't collide
# with those, but it might have a package.json (docs tooling, linting),
# so it must come before the npm-based toolchains to win correctly.
PYTHON = Toolchain(
    name="python (pytest)",
    marker_files=("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
    commands={
        "build_cmd": "python -m pytest --collect-only -q",
        "test_compile_cmd": "python -m pytest --collect-only -q",
        "test_cmd": "python -m pytest",
        "test_filter_cmd": "python -m pytest {filter}",
        "fmt_fix_cmd": "ruff format .",
        "clippy_fix_cmd": "ruff check --fix .",
        "fmt_check_cmd": "ruff format --check .",
        "clippy_cmd": "ruff check .",
    },
    evidence_binary="pytest",
    # pytest only ever runs tests - like ctest, no subcommand gate needed.
    evidence_subcommands=None,
    test_output_signal_pattern=r"FAILED|ERROR|====.*(passed|failed|error)",
)

# SvelteKit specifically (not generic TypeScript/Node) per the project
# types this was built for - svelte-check is the natural "does this
# typecheck" gate, which is more useful evidence than a plain build.
SVELTEKIT = Toolchain(
    name="typescript (sveltekit/npm)",
    marker_files=("svelte.config.js", "svelte.config.ts"),
    commands={
        "build_cmd": "npm run build",
        "test_compile_cmd": "npx svelte-check",
        "test_cmd": "npm test",
        "test_filter_cmd": "npm test -- {filter}",
        "fmt_fix_cmd": "npx prettier --write .",
        "clippy_fix_cmd": "npx eslint . --fix",
        "fmt_check_cmd": "npx prettier --check .",
        "clippy_cmd": "npx eslint .",
    },
    evidence_binary="npm",
    evidence_subcommands=frozenset({"test"}),
    test_output_signal_pattern=r"✓|✗|FAIL|PASS|passing|failing|Tests:\s",
)

# Generic Node/TypeScript fallback when package.json exists but no
# svelte.config.* does - same shape, no svelte-check assumption.
TYPESCRIPT = Toolchain(
    name="typescript/node (npm)",
    marker_files=("package.json",),
    commands={
        "build_cmd": "npm run build",
        "test_compile_cmd": "npx tsc --noEmit",
        "test_cmd": "npm test",
        "test_filter_cmd": "npm test -- {filter}",
        "fmt_fix_cmd": "npx prettier --write .",
        "clippy_fix_cmd": "npx eslint . --fix",
        "fmt_check_cmd": "npx prettier --check .",
        "clippy_cmd": "npx eslint .",
    },
    evidence_binary="npm",
    evidence_subcommands=frozenset({"test"}),
    test_output_signal_pattern=r"✓|✗|FAIL|PASS|passing|failing|Tests:\s",
)

# Priority order for detection - first marker-file match wins. Bazel
# before Cargo/CMake (monorepo-wrapping, see BAZEL's docstring note);
# Python before SvelteKit/TypeScript (a Python project may have a
# package.json for docs/linting tooling but is not a Node project);
# SvelteKit before generic TypeScript (more specific marker).
TOOLCHAINS: tuple[Toolchain, ...] = (BAZEL, RUST, CMAKE, PYTHON, SVELTEKIT, TYPESCRIPT)


def detect_toolchain(root: Path = Path(".")) -> Toolchain | None:
    """
    Returns the first toolchain in TOOLCHAINS whose marker file exists
    directly under `root`. Returns None if nothing matches - callers
    should fall back to a hardcoded default (see pipeline_lib) rather
    than fail outright, since an unrecognized project can still work
    fine off a fully project-local .dev-pipeline.toml.
    """
    for toolchain in TOOLCHAINS:
        if any((root / marker).is_file() for marker in toolchain.marker_files):
            return toolchain
    return None
