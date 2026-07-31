"""
tools - local file read/write/list tool layer for model tool calls.

No MCP, no subprocess, no external server - these execute in-process
against the project's working directory (cwd), with the same
path-confinement guard regardless of which tool is called. Intended for
use with ai_client.run_with_tools, which expects an executor of the
shape produced by make_executor() below.
"""

import os
import re
from pathlib import Path
from typing import Callable

from . import verbosity

log = verbosity.get_logger(__name__)


class ToolError(RuntimeError):
    """Raised for any tool-execution failure. Callers turn this into a
    string error result for the model, not a hard crash - a model that
    tries to read a nonexistent file should be told so and get to
    recover, not blow up the whole pipeline step."""


class PipelineAbort(RuntimeError):
    """
    Base class for pseudo-tool calls that are never turned into a string
    result fed back to the model - the call itself is the fail-fast
    signal. Left to propagate out of ai_client.run_with_tools to the
    caller, which catches this alongside AIError.
    """


class ClarificationNeeded(PipelineAbort):
    """
    Raised when the model calls the ask_user_prompt pseudo-tool. These
    pipelines are single-shot and non-interactive, so there is no human
    to actually answer mid-task - receiving the call is itself the
    fail-fast signal, with the model's question as the failure reason.
    """




def _safe_path(path_str: str) -> Path:
    """
    Resolve `path_str` relative to cwd and refuse anything that would
    escape the project root - absolute paths or '..' traversal. This is
    model-generated input, untrusted in the same sense as any external
    input that ends up driving a filesystem write.
    """
    cwd = Path.cwd().resolve()
    path = Path(path_str)
    if path.is_absolute() or ".." in path.parts:
        raise ToolError(f"path escapes project root: {path_str}")
    resolved = (cwd / path).resolve()
    if resolved != cwd and cwd not in resolved.parents:
        raise ToolError(f"path escapes project root: {path_str}")
    return resolved


def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    """
    Read a file's content. With no range, returns the raw full text plus
    a trailing line-count note (e.g. so a model sizing up a file before
    deciding how to read/edit it - the most common reason one would
    otherwise reach for `wc -l` - gets the count for free, no extra tool
    call needed). With start_line/end_line (1-indexed, inclusive; either
    may be omitted to mean "to the start"/"to the end"), returns just
    that slice with line numbers prefixed, so a model can page through a
    large file without spending its whole context budget on one
    read_file call. A negative start_line means "N lines from the end"
    (tail-style) - e.g. start_line=-20 returns the last 20 lines;
    end_line is ignored in that mode, since "last N to line X" isn't a
    request that comes up in practice.
    """
    resolved = _safe_path(path)
    if not resolved.is_file():
        raise ToolError(f"not found: {path}")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    if start_line is None and end_line is None:
        total = len(text.splitlines())
        return f"{text}\n\n({total} line{'s' if total != 1 else ''} total)"

    lines = text.splitlines()
    total = len(lines)

    if start_line is not None and start_line < 0:
        start = max(1, total + start_line + 1)
        end = total
    else:
        start = start_line if start_line is not None else 1
        # Validate start against the file's actual bounds before
        # defaulting end_line to total - otherwise an out-of-range
        # start_line with no end_line produces a confusing "end_line
        # must be >= start_line" error instead of the real "start_line
        # is beyond end of file" one.
        if start < 1:
            raise ToolError(f"start_line must be >= 1, got {start}")
        if start > total:
            raise ToolError(f"start_line {start} is beyond end of file ({total} lines)")
        end = end_line if end_line is not None else total
        if end < start:
            raise ToolError(f"end_line ({end}) must be >= start_line ({start})")
        end = min(end, total)

    numbered = "\n".join(f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1))
    return f"(showing lines {start}-{end} of {total} total)\n{numbered}"


# Directories that are almost never useful to search and can be huge
# (VCS internals, dependency trees, build output, caches) - pruned
# before os.walk descends into them, not just filtered after the fact.
SEARCH_IGNORED_DIR_NAMES = {
    ".git", ".hg", ".svn",
    "node_modules", "target", "dist", "build",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", ".tox", "htmlcov", ".eggs",
}

DEFAULT_SEARCH_MAX_RESULTS = 200
HARD_SEARCH_MAX_RESULTS = 500


def search_files(
    pattern: str,
    path: str = ".",
    regex: bool = False,
    max_results: int = DEFAULT_SEARCH_MAX_RESULTS,
) -> str:
    """
    Recursively search file contents under `path` for `pattern`, like
    grep - plain substring match by default, or a regular expression if
    `regex` is True. Pure Python (re + os.walk), no subprocess and no
    shell, so it works identically regardless of platform or whether a
    real grep binary is installed. Binary files are skipped via a
    strict-UTF-8 decode check; common non-source directories are pruned
    before descending into them.
    """
    resolved = _safe_path(path)
    if not resolved.is_dir():
        raise ToolError(f"not a directory: {path}")

    if regex:
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}")
        line_matches = compiled.search
    else:
        line_matches = lambda line: pattern in line  # noqa: E731

    max_results = max(1, min(max_results, HARD_SEARCH_MAX_RESULTS))
    cwd = Path.cwd().resolve()
    results: list[str] = []
    truncated = False

    for root, dirnames, filenames in os.walk(resolved):
        dirnames[:] = sorted(d for d in dirnames if d not in SEARCH_IGNORED_DIR_NAMES)
        for filename in sorted(filenames):
            file_path = Path(root) / filename
            try:
                text = file_path.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            rel = file_path.relative_to(cwd).as_posix()
            for line_number, line in enumerate(text.splitlines(), start=1):
                if line_matches(line):
                    results.append(f"{rel}:{line_number}:{line.strip()}")
                    if len(results) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not results:
        return f"(no matches for {pattern!r} under {path})"
    output = "\n".join(results)
    if truncated:
        output += f"\n(showing first {max_results} matches - narrow your pattern or path)"
    return output


def write_file(path: str, content: str) -> str:
    resolved = _safe_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote {path} ({len(content)} bytes)"


def write_file_block(path: str) -> Callable[[str], str]:
    """
    Returns a block (str -> str) that writes its input to `path` via
    write_file and returns the input unchanged, so a write can be a
    pipeline stage - write_file_block(PLAN_FILE)(plan_text) - instead of
    a tool call inside an agentic step.
    """
    def block(content: str) -> str:
        log.info("   %s", write_file(path, content))
        return content
    return block


def list_dir(path: str = ".") -> str:
    """
    Lists immediate children of `path`. Directories are listed bare
    (name/), same as before; files get a trailing line-count annotation
    (or a byte-size one for files that aren't valid UTF-8 text, e.g.
    binaries) - so a model deciding what's worth a read_file call can see
    roughly how big each candidate is first, without a separate tool call
    or reaching for `wc`/`ls -la`.
    """
    resolved = _safe_path(path)
    if not resolved.is_dir():
        raise ToolError(f"not a directory: {path}")

    def describe(entry: Path) -> str:
        if entry.is_dir():
            return f"{entry.name}/"
        try:
            text = entry.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            size = entry.stat().st_size
            return f"{entry.name}  ({size} bytes, binary)"
        lines = len(text.splitlines())
        return f"{entry.name}  ({lines} line{'s' if lines != 1 else ''})"

    entries = sorted(resolved.iterdir(), key=lambda p: p.name)
    described = [describe(entry) for entry in entries]
    return "\n".join(described) if described else "(empty)"


def summarize_tool_call(name: str, args: dict) -> str:
    """
    One-line human-readable summary of a tool call for console logging -
    the raw name(args) form is noisy (full file contents for write_file,
    repeated path/range tuples) and not what anyone watching the run
    actually wants to see.
    """
    path = args.get("path")
    if name == "read_file":
        if path is None:
            return "Read file"
        start, end = args.get("start_line"), args.get("end_line")
        if start is None and end is None:
            return f"Read {path}"
        if start is not None and start < 0:
            return f"Read {path} (last {-start} lines)"
        return f"Read {path} (lines {start or 1}-{end or '?'})"
    if name == "write_file":
        return f"Write {path}" if path else "Write file"
    if name == "list_dir":
        return f"List {path or '.'}"
    if name == "search_files":
        pattern = args.get("pattern", "")
        return f"Search for {pattern!r} in {args.get('path', '.')}"
    if name == "ask_user_prompt":
        return "Ask user a clarifying question"
    if name == ASK_USER_QUESTION_TOOL_NAME:
        return f"Ask: {args.get('question', '(no question)')}"
    if name == "run_command":
        return f"Attempt to run command: {args.get('command', '')}"
    return f"{name}({args})"


# ---------------------------------------------------------------------------
# OpenAI-style tool schemas
# ---------------------------------------------------------------------------

READ_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a file's content, path relative to the project root. "
            "With no start_line/end_line, returns the full current "
            "content. For a large file, pass start_line and/or end_line "
            "(1-indexed, inclusive) to read just that slice instead - "
            "the result comes back with line numbers prefixed. Pass a "
            "negative start_line for a tail-style read of the last N "
            "lines (e.g. start_line=-20 for the last 20 lines). Prefer a "
            "full read for files you're likely to need entirely; use a "
            "range when you only need to check or quote a specific part "
            "of something large."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {
                    "type": "integer",
                    "description": "First line to read, 1-indexed, inclusive. Omit to start from the beginning. Negative means 'N lines from the end' (tail-style); end_line is ignored in that case.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read, 1-indexed, inclusive. Omit to read to the end.",
                },
            },
            "required": ["path"],
        },
    },
}

LIST_DIR_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": (
            "List entries in a directory, path relative to the project root. "
            "Defaults to the project root. Directories are listed bare "
            "(name/); files are annotated with a line count (or byte size "
            "for non-text/binary files), so you can size up candidates "
            "before deciding what's worth a read_file call."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        },
    },
}

SEARCH_FILES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
            "Search file contents recursively under a directory, like "
            "grep -rn. Plain substring match by default; pass regex=true "
            "for a regular expression instead. Common non-source "
            "directories (.git, node_modules, target, __pycache__, etc.) "
            "and binary files are skipped automatically. Results are "
            "capped (default 200, max 500 matching lines) - narrow your "
            "pattern or path if you hit the cap rather than relying on "
            "the cap to find everything."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": "Directory to search under, relative to the project root. Defaults to the project root.",
                },
                "regex": {
                    "type": "boolean",
                    "description": "Treat pattern as a regular expression instead of a plain substring. Defaults to false.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum matching lines to return (default 200, hard cap 500).",
                },
            },
            "required": ["pattern"],
        },
    },
}

WRITE_FILE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write (overwrite) a file's complete contents, path relative "
            "to the project root. Creates parent directories as needed. "
            "Always pass the full file content, never a diff or excerpt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
}

ASK_USER_PROMPT_TOOL_NAME = "ask_user_prompt"

ASK_USER_PROMPT_SCHEMA = {
    "type": "function",
    "function": {
        "name": ASK_USER_PROMPT_TOOL_NAME,
        "description": (
            "Call this ONLY if you genuinely cannot proceed without human "
            "clarification - e.g. the plan or ticket is ambiguous in a way "
            "you cannot reasonably resolve from context, or required "
            "information is missing and not discoverable via read_file or "
            "list_dir. This pipeline is single-shot and non-interactive: "
            "calling this tool immediately aborts the entire run with your "
            "question as the failure reason. There is no human available to "
            "answer and no retry afterwards. Only call this as a last "
            "resort, after attempting to resolve the ambiguity yourself per "
            "your other instructions - do not call it speculatively or to "
            "confirm something you could reasonably infer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question blocking progress.",
                }
            },
            "required": ["question"],
        },
    },
}

ASK_USER_QUESTION_TOOL_NAME = "ask_user_question"

ASK_USER_QUESTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": ASK_USER_QUESTION_TOOL_NAME,
        "description": (
            "Ask the human a single, specific question and wait for their "
            "real answer before continuing - this session is genuinely "
            "interactive, unlike ask_user_prompt elsewhere in this "
            "pipeline. Use this liberally whenever a requirement is "
            "missing, ambiguous, or would materially change the scope or "
            "acceptance criteria, and whenever you cannot resolve the gap "
            "yourself by reading the codebase - asking is expected and "
            "cheap here, not a last resort. Ask one focused question at a "
            "time rather than bundling several into one call, so the "
            "human can answer each on its own merits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The specific question to ask the human.",
                }
            },
            "required": ["question"],
        },
    },
}

RUN_COMMAND_TOOL_NAME = "run_command"

RUN_COMMAND_SCHEMA = {
    "type": "function",
    "function": {
        "name": RUN_COMMAND_TOOL_NAME,
        "description": (
            "Run a command-line command. NOT SUPPORTED: there is no shell "
            "behind this tool, ever - calling it returns an error instead of "
            "running anything, every time. Build and test verification is "
            "handled by the caller between steps, not by you. For the common "
            "reasons models reach for a shell: use search_files instead of "
            "grep/find, use read_file's start_line/end_line (including a "
            "negative start_line for tail-style reads) instead of head/tail/ "
            "wc, and use list_dir instead of ls - none of these need cd "
            "first, every tool here takes an explicit path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command that would have been run.",
                }
            },
            "required": ["command"],
        },
    },
}

# Every step gets these alongside its other tools - ask_user_prompt gives
# the model an explicit way to signal "I'm stuck" instead of guessing
# (a real abort - see ClarificationNeeded), and run_command is offered
# but always refused with a recoverable error message pointing at the
# real alternative (read_file/list_dir/search_files) - a model reaching
# for a shell isn't a deliberate "I need help" signal the way
# ask_user_prompt is, just a model not knowing what's available, so one
# bad attempt shouldn't abort the whole run the way ClarificationNeeded
# does.
READ_ONLY_TOOLS = [
    READ_FILE_SCHEMA,
    LIST_DIR_SCHEMA,
    SEARCH_FILES_SCHEMA,
    ASK_USER_PROMPT_SCHEMA,
    RUN_COMMAND_SCHEMA,
]

READ_WRITE_TOOLS = [
    READ_FILE_SCHEMA,
    LIST_DIR_SCHEMA,
    SEARCH_FILES_SCHEMA,
    WRITE_FILE_SCHEMA,
    ASK_USER_PROMPT_SCHEMA,
    RUN_COMMAND_SCHEMA,
]

# For genuinely interactive sessions (a human present at the terminal for
# the whole run) - swaps the abort-on-ask
# ASK_USER_PROMPT_SCHEMA for ASK_USER_QUESTION_SCHEMA, which make_executor
# answers for real when interactive=True instead of raising
# ClarificationNeeded. No write_file - this is exploration + discussion,
# never a place that edits repo files.
EXPLORE_TOOLS = [
    READ_FILE_SCHEMA,
    LIST_DIR_SCHEMA,
    SEARCH_FILES_SCHEMA,
    ASK_USER_QUESTION_SCHEMA,
    RUN_COMMAND_SCHEMA,
]


def make_executor(
    written_paths: list[str] | None = None,
    allow_write: bool = True,
    protected_paths: set[str] | None = None,
    preloaded_paths: set[str] | None = None,
    interactive: bool = False,
):
    """
    Build a tool_executor(name, args) -> str for ai_client.run_with_tools.

    written_paths: if given, every successful write_file call appends its
        path here, so callers can track what was written without
        re-parsing model output text.
    allow_write: if False, write_file calls are rejected outright - a
        defense-in-depth backstop for read-only steps, independent of
        whether write_file is even in the tool schema offered to the
        model (schemas constrain what's offered; this constrains what's
        actually executed).
    protected_paths: write_file calls targeting any of these paths are
        rejected without writing - e.g. so an Implement step can't
        overwrite the test files it's supposed to be satisfying.
    preloaded_paths: paths whose content the caller already embedded
        directly in the initial prompt (e.g. the ticket), rather than
        making the model fetch them via read_file. Seeded as fully-read,
        so a redundant read_file call on one of these still gets the
        short dedup note instead of resending content the model was
        already given - including a partial-range read, since having
        the whole file already covers any slice of it.
    interactive: if True, ask_user_question calls print the question and
        block on real terminal input (via input()), returning the human's
        answer as the tool result so the model's conversation continues -
        for a script where a human is genuinely present the whole run
        (e.g. push_ticket --explore). If False (default), ask_user_question is
        rejected the same way write_file is when allow_write=False - this
        executor is meant to answer for real or not offer the tool at all,
        never to silently no-op a live question.

    Every chat-completions turn resends the *entire* message history,
    tool results included - the API has no server-side session state.
    A model re-reading the same file at turn 3 and turn 6 doesn't just
    double that file's tokens once; it doubles them in every turn's
    payload from turn 6 onward for the rest of the conversation. To stop
    that, reads are cached per executor instance: a full read of a path
    already fully read gets a short note instead of the content again,
    and a partial range already read at that exact range does too. A
    *different* range on a path that's only been partially read is not
    deduped - the model genuinely doesn't have that part yet. write_file
    invalidates all cache state for its own path, since the content
    genuinely changed and a later read should see it.
    """
    full_read_paths: set[str] = set(preloaded_paths or ())
    partial_ranges: set[tuple[str, int | None, int | None]] = set()

    def executor(name: str, args: dict) -> str:
        if name == ASK_USER_PROMPT_TOOL_NAME:
            question = args.get("question", "(no question provided)")
            raise ClarificationNeeded(
                f"model requested human clarification mid-task: {question}"
            )
        if name == ASK_USER_QUESTION_TOOL_NAME:
            question = args.get("question", "(no question provided)")
            if not interactive:
                return "ERROR: ask_user_question is not available in this step"
            print(f"\n? {question}")
            try:
                answer = input("> ").strip()
            except EOFError:
                answer = ""
            return answer if answer else "(human gave no answer - proceed with your own best judgement)"
        if name == RUN_COMMAND_TOOL_NAME:
            command = args.get("command", "(no command provided)")
            log.warning("-- Refused run_command(%r) - recoverable, not aborting.", command)
            return (
                "ERROR: run_command is not supported - there is no shell behind this tool, "
                "ever, calling it again will not work either. Use search_files instead of "
                "grep/find, use read_file's start_line/end_line (including a negative "
                "start_line for tail-style reads) instead of head/tail/wc, and use list_dir "
                "instead of ls - none of these need cd first, every tool here takes an "
                "explicit path."
            )
        try:
            if name == "read_file":
                path = args["path"]
                start_line = args.get("start_line")
                end_line = args.get("end_line")
                range_key = (path, start_line, end_line)

                if path in full_read_paths or range_key in partial_ranges:
                    return (
                        f"(duplicate read_file(\"{path}\") - you already have "
                        f"this content (or the whole file, which covers it), "
                        f"either from the initial prompt or an earlier "
                        f"read_file call in this conversation; not re-sent to "
                        f"save context)"
                    )
                content = read_file(path, start_line, end_line)
                if start_line is None and end_line is None:
                    full_read_paths.add(path)
                else:
                    partial_ranges.add(range_key)
                return content
            if name == "list_dir":
                return list_dir(args.get("path", "."))
            if name == "search_files":
                return search_files(
                    args["pattern"],
                    args.get("path", "."),
                    args.get("regex", False),
                    args.get("max_results", DEFAULT_SEARCH_MAX_RESULTS),
                )
            if name == "write_file":
                if not allow_write:
                    return "ERROR: write_file is not available in this step"
                path = args["path"]
                if protected_paths and path in protected_paths:
                    return f"ERROR: refused to overwrite protected file: {path}"
                result = write_file(path, args["content"])
                full_read_paths.discard(path)
                partial_ranges.difference_update(
                    {key for key in partial_ranges if key[0] == path}
                )
                if written_paths is not None:
                    written_paths.append(path)
                return result
            return f"ERROR: unknown tool: {name}"
        except ToolError as e:
            return f"ERROR: {e}"
        except KeyError as e:
            return f"ERROR: missing required argument: {e}"

    return executor
