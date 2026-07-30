"""
render - console markdown rendering for model output.

Model responses (plan summaries, validator/reviewer verdicts) are
markdown - headings, bold, tables, code blocks - written per the
prompts' own conventions. Printing that raw is readable but ugly;
this renders it properly via `rich`.

Wrapping stdout in a UTF-8 TextIOWrapper works around a real failure
mode on Windows: rich's legacy-console code path encodes through
whatever codepage the terminal is actually using (often cp1252, not
UTF-8), and the prompts' own "🤖" marker is enough to crash that path
with UnicodeEncodeError. If rendering still fails for any other reason,
fall back to a plain print rather than taking the whole pipeline down
over a presentation nicety.
"""

import io
import sys

from rich.console import Console
from rich.markdown import Markdown

_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_console = Console(file=_stdout)


def render_markdown(text: str) -> None:
    try:
        _console.print(Markdown(text))
    except Exception:
        print(text)


def print_line(text: str = "") -> None:
    """
    Unconditional stdout output, independent of --log-level. Used for a
    script's actual result (final summary, success/failure line, token
    usage) - the thing the script exists to report, not a progress or
    diagnostic message that's fine to filter out at a quieter level.

    Writes through the same UTF-8 TextIOWrapper render_markdown uses
    (not a bare print(), which encodes through sys.stdout's default
    encoding - often cp1252 on Windows, not UTF-8) - model output
    routinely contains characters (em dashes, arrows like the U+2192 in
    a propose-ticket-edit.py diff line, emoji) that cp1252 can't encode
    at all, which previously crashed the whole script with
    UnicodeEncodeError on the very last line of otherwise-successful
    output. errors="replace" means a genuinely unmappable character
    degrades to a substitution glyph instead of crashing.
    """
    try:
        _stdout.write(text)
        _stdout.write("\n")
        _stdout.flush()
    except Exception:
        print(text, flush=True)
