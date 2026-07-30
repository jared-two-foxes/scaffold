"""
repo_context - mechanically-generated repo orientation block, seeded
into AI step prompts so each one doesn't have to (re)discover the same
basic facts (toolchain, layout, module boundaries) via its own
list_dir/search_files tool calls before it can do anything
criterion-specific.

Phase 1, mechanical only: toolchain detection (reuses toolchains.py),
a depth-limited directory tree, and best-effort module-root discovery.
Deliberately no AI-generated "important interfaces" summary yet - that's
future work, gated on whether this cheap version alone measurably cuts
tool-call turns in steps that orient from scratch every time (see
resolve-ticket.py's per-criterion loop).
"""

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import toolchains
from . import tools

DEFAULT_TREE_DEPTH = 3

# Same set tools.search_files already prunes by - VCS internals,
# dependency trees, build output, caches are never useful orientation
# context and can be huge.
PRUNED_DIR_NAMES = tools.SEARCH_IGNORED_DIR_NAMES

# Candidate source roots checked for module_roots, in no particular
# priority order - whichever exist contribute their immediate
# subdirectories. Covers Rust workspaces (libs/, src/), generic Node/TS
# layouts (src/, lib/), and this repo's own libs/ pattern without
# writing per-toolchain parsing (e.g. Cargo.toml [workspace] members
# globs) that would only pay off for one toolchain.
MODULE_ROOT_CANDIDATES = ("src", "lib", "libs")

# Human-authored files that exist specifically to tell an agent how a
# codebase works (conventions, gotchas, "don't do X") - higher signal
# per token than a directory tree, and free to find (fixed filename
# check, not a search). Checked in this order; ALL that exist are
# included, not just the first match - AGENTS.md and CLAUDE.md commonly
# carry different content (general vs. tool-specific instructions).
CONVENTION_DOC_CANDIDATES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "CONTRIBUTING.md")

# Unlike the tree (naturally bounded by depth), a convention doc has no
# inherent size limit - cap it so one large file can't silently eat an
# unbounded amount of every prompt's budget.
CONVENTION_DOC_MAX_CHARS = 4000

TICKET_EVIDENCE_MAX_TOKENS = 12
TICKET_EVIDENCE_MATCHES_PER_TOKEN = 8
TICKET_EVIDENCE_MAX_CHARS = 6000

BACKTICK_TOKEN_RE = re.compile(r"`([^`\n]+)`")
ENV_VAR_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
DOTTED_SYMBOL_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
)
PASCAL_SYMBOL_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*\b")
SNAKE_SYMBOL_RE = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b")

NOISY_TICKET_TOKENS = {
    "AC",
    "Acceptance",
    "Criteria",
    "Default",
    "Definition",
    "DoD",
    "Done",
    "Description",
    "Edge",
    "Field",
    "Files",
    "High",
    "Labels",
    "Medium",
    "Missing",
    "None",
    "Option",
    "Priority",
    "Requirements",
    "Some",
    "State",
    "String",
    "Summary",
    "Todo",
    "URL",
    "Update",
    "Updated",
    "cargo",
    "clippy",
    "fmt",
    "test",
    "tests",
}

NOISY_BACKTICK_PREFIXES = ("cargo ", "npm ", "npx ", "bazel ", "ctest ", "fmt ", "clippy ")

PATH_SUFFIXES = (
    ".rs", ".py", ".ts", ".tsx", ".js", ".jsx", ".svelte", ".toml",
    ".json", ".yaml", ".yml", ".md", ".sql",
)


@dataclass(frozen=True)
class RepoContext:
    toolchain_name: str
    tree: str
    module_roots: list[str]
    convention_docs: list[tuple[str, str]]


@dataclass(frozen=True)
class TicketEvidenceEntry:
    token: str
    kind: str
    result: str
    has_matches: bool


@dataclass(frozen=True)
class TicketEvidenceSeed:
    entries: list[TicketEvidenceEntry]
    searched_tokens: list[str]


@contextmanager
def _pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _is_path_like(token: str) -> bool:
    return (
        "/" in token
        or "\\" in token
        or token.endswith(PATH_SUFFIXES)
    )


def _clean_ticket_token(token: str) -> str | None:
    token = token.strip().strip(".,:;()[]{}")
    if not token:
        return None
    lowered = token.lower()
    if token in NOISY_TICKET_TOKENS or lowered in NOISY_TICKET_TOKENS:
        return None
    if any(lowered.startswith(prefix) for prefix in NOISY_BACKTICK_PREFIXES):
        return None
    if " " in token and not _is_path_like(token):
        return None
    if len(token) < 3 and not token.isupper():
        return None
    return token


def _ticket_token_kind(token: str) -> str:
    if _is_path_like(token):
        return "path"
    if ENV_VAR_RE.fullmatch(token):
        return "env"
    if DOTTED_SYMBOL_RE.fullmatch(token):
        return "dotted-symbol"
    if SNAKE_SYMBOL_RE.fullmatch(token):
        return "snake-symbol"
    return "symbol"


def extract_ticket_evidence_tokens(
    ticket_content: str,
    max_tokens: int = TICKET_EVIDENCE_MAX_TOKENS,
) -> list[str]:
    """
    Pull high-signal code/search tokens out of ticket prose, preserving
    ticket order and staying deliberately mechanical. This is only an
    orientation accelerator for Planner-Narrower, not a semantic parse
    of the ticket.
    """
    token_candidates: list[str] = []
    token_candidates.extend(BACKTICK_TOKEN_RE.findall(ticket_content))
    token_candidates.extend(ENV_VAR_RE.findall(ticket_content))
    token_candidates.extend(DOTTED_SYMBOL_RE.findall(ticket_content))
    token_candidates.extend(PASCAL_SYMBOL_RE.findall(ticket_content))
    token_candidates.extend(SNAKE_SYMBOL_RE.findall(ticket_content))

    tokens: list[str] = []
    seen: set[str] = set()
    for candidate in token_candidates:
        token = _clean_ticket_token(candidate)
        if token is None or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= max_tokens:
            break
    return tokens


def _build_tree_lines(root: Path, depth: int, prefix: str = "") -> list[str]:
    if depth <= 0:
        return []
    try:
        entries = sorted(
            root.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )
    except OSError:
        return []
    lines = []
    for entry in entries:
        if entry.is_dir():
            if entry.name in PRUNED_DIR_NAMES:
                continue
            lines.append(f"{prefix}{entry.name}/")
            lines.extend(_build_tree_lines(entry, depth - 1, prefix + "  "))
        else:
            lines.append(f"{prefix}{entry.name}")
    return lines


def _gather_tree(root: Path, depth: int) -> str:
    lines = _build_tree_lines(root, depth)
    return "\n".join(lines) if lines else "(empty)"


def _gather_module_roots(root: Path) -> list[str]:
    module_roots: list[str] = []
    for candidate in MODULE_ROOT_CANDIDATES:
        candidate_path = root / candidate
        if not candidate_path.is_dir():
            continue
        for entry in sorted(candidate_path.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir() and entry.name not in PRUNED_DIR_NAMES:
                module_roots.append(f"{candidate}/{entry.name}")
    return module_roots


def _gather_convention_docs(root: Path) -> list[tuple[str, str]]:
    docs: list[tuple[str, str]] = []
    for filename in CONVENTION_DOC_CANDIDATES:
        path = root / filename
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > CONVENTION_DOC_MAX_CHARS:
            omitted = len(content) - CONVENTION_DOC_MAX_CHARS
            content = content[:CONVENTION_DOC_MAX_CHARS] + f"\n... (truncated, {omitted} chars omitted)"
        docs.append((filename, content))
    return docs


def gather_repo_context(root: Path = Path("."), depth: int = DEFAULT_TREE_DEPTH) -> RepoContext:
    toolchain = toolchains.detect_toolchain(root) or toolchains.RUST
    return RepoContext(
        toolchain_name=toolchain.name,
        tree=_gather_tree(root, depth),
        module_roots=_gather_module_roots(root),
        convention_docs=_gather_convention_docs(root),
    )


def gather_ticket_evidence_seed(
    ticket_content: str,
    root: Path = Path("."),
) -> TicketEvidenceSeed:
    """
    Search the current repo for a bounded set of ticket-derived code
    tokens. No AI, no writes: this is just the host doing the cheap,
    obvious first searches so the merged plan+narrow step starts closer
    to the evidence it needs.
    """
    tokens = extract_ticket_evidence_tokens(ticket_content)
    entries: list[TicketEvidenceEntry] = []
    root = root.resolve()
    with _pushd(root):
        for token in tokens:
            kind = _ticket_token_kind(token)
            result = tools.search_files(
                token,
                ".",
                regex=False,
                max_results=TICKET_EVIDENCE_MATCHES_PER_TOKEN,
            )
            has_matches = not result.startswith("(no matches ")
            if has_matches or kind in {"env", "path"}:
                entries.append(TicketEvidenceEntry(token, kind, result, has_matches))
    return TicketEvidenceSeed(entries=entries, searched_tokens=tokens)


def render_repo_context_block(ctx: RepoContext, depth: int = DEFAULT_TREE_DEPTH) -> str:
    module_roots_line = (
        ", ".join(ctx.module_roots) if ctx.module_roots else "(none found)"
    )
    block = (
        f"## Repo Context\n"
        f"Toolchain: {ctx.toolchain_name}\n"
        f"Module roots: {module_roots_line}\n\n"
        f"### Directory tree (depth {depth}, common build/VCS/dependency dirs pruned)\n"
        f"{ctx.tree}"
    )
    if ctx.convention_docs:
        docs_block = "\n\n".join(
            f"#### {filename}\n{content}" for filename, content in ctx.convention_docs
        )
        block += f"\n\n### Convention docs found at the project root\n{docs_block}"
    return block


def render_ticket_evidence_seed_block(
    seed: TicketEvidenceSeed,
    max_chars: int = TICKET_EVIDENCE_MAX_CHARS,
) -> str:
    if not seed.searched_tokens:
        return (
            "## Ticket Evidence Seed\n"
            "(no high-signal ticket identifiers found for host-side search)"
        )
    if not seed.entries:
        return (
            "## Ticket Evidence Seed\n"
            "Host-side searches for high-signal ticket identifiers found no "
            "matching code. This is preliminary orientation only; verify with "
            "tools before judging criteria."
        )

    header = (
        "## Ticket Evidence Seed\n"
        "Preliminary host-side literal search hits for high-signal ticket "
        "identifiers. Use these as orientation only; verify with tools before "
        "marking any criterion PASS.\n"
    )
    blocks: list[str] = [header]
    truncated = False
    for entry in seed.entries:
        block = f"\n### `{entry.token}` ({entry.kind})\n{entry.result}\n"
        current_len = sum(len(part) for part in blocks)
        if current_len + len(block) > max_chars:
            truncated = True
            break
        blocks.append(block)

    rendered = "".join(blocks).rstrip()
    if truncated:
        rendered += "\n\n... (ticket evidence seed truncated to stay within prompt budget)"
    return rendered
