"""
ai_client - shared AI invocation library for scripted (non-chat) prompts.

Talks to opencode zen's OpenAI-compatible chat-completions endpoint by
default, or another registered provider (see PROVIDERS) by prefixing
the model id with "<provider>:" (e.g. "ollama:llama3.1",
"copilot:claude-sonnet-4.6", "opencode:gpt-5.4-mini"). A bare model id
with no recognized prefix always means opencode zen - every existing
caller that passes a plain model string ("gpt-5.4-mini", "default", ...)
keeps working unchanged; the "opencode:" prefix is available for
callers that want to name it explicitly the same way every other
provider is named, not a requirement.

Two invocation modes:
  run_prompt()      - single request, plain text response. No tools.
  run_with_tools()  - multi-turn round trip using the OpenAI "tools"
                      (function-calling) param. The caller supplies a
                      tool schema list and an executor; this function
                      loops sending the executor's results back to the
                      model until it returns a plain text answer with no
                      further tool calls. No turn limit - a model doing
                      real exploration (reading several files, searching,
                      writing several outputs) can legitimately need many
                      turns; the pseudo-tools (ask_user_prompt,
                      run_command) are the actual stuck/wrong-direction
                      signals to watch for, not turn count.

Auth: the API key is read from ~/.secrets/opencode-key (same convention
as fetch_ticket.py's Linear key), falling back to $OPENCODE_ZEN_API_KEY
if that file doesn't exist.

Model selection is a parameter to these functions, not an env var -
callers decide which model to request per call.

Token usage: every response's "usage" field (prompt_tokens,
completion_tokens) is accumulated per-model into the module-level
`usage` UsageTracker instance for the lifetime of the process. Callers
print `ai_client.usage` whenever they want a running or final total,
including an estimated $ cost if the model's rate is in
ticket_pipeline/lib/model-pricing.toml - models with no entry there show token
counts with no fabricated cost.
"""

import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import verbosity

log = verbosity.get_logger(__name__)


class AIError(RuntimeError):
    """Raised for any invocation failure. Let it propagate to die()."""


class StepBudgetExceeded(AIError):
    """
    Raised by run_with_tools when a turn-count or cumulative-cost ceiling
    is hit. Subclasses AIError so every existing call site's
    `except AIError` still catches and dies on it with no code changes -
    but it is NOT a transient/retryable condition: pipeline_lib's
    run_ai_step_with_retry checks for this subclass explicitly and
    re-raises it immediately rather than retrying, since retrying a
    budget that's already exhausted just burns the budget further.
    """


@dataclass
class AIResult:
    text: str


@dataclass
class ModelUsage:
    """Prompt/completion token total for a single model id."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, usage: dict) -> None:
        self.prompt_tokens += usage.get("prompt_tokens", 0) or 0
        self.completion_tokens += usage.get("completion_tokens", 0) or 0


@dataclass
class UsageTracker:
    """
    Running per-model token totals for the lifetime of the process -
    accumulated across every request a script makes (each
    run_with_tools turn is its own request, since the full message
    history is resent every turn), not per-call. Read via
    ai_client.usage from the calling script to report a final total.

    Tracked per model (not just one running total) because a single
    script run could in principle call more than one model, and
    pricing is looked up per model id.
    """

    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    def add(self, model: str, response_usage: dict) -> None:
        self.by_model.setdefault(model, ModelUsage()).add(response_usage)

    @property
    def total_tokens(self) -> int:
        return sum(m.total_tokens for m in self.by_model.values())

    @property
    def prompt_tokens(self) -> int:
        return sum(m.prompt_tokens for m in self.by_model.values())

    @property
    def completion_tokens(self) -> int:
        return sum(m.completion_tokens for m in self.by_model.values())

    def total_cost_usd(self) -> tuple[float, list[str]]:
        """
        Returns (known_cost_so_far, [model ids with no pricing entry]).
        known_cost_so_far only sums models that have a pricing entry -
        it is a lower bound, not a total, whenever the second element
        is non-empty.
        """
        pricing = load_pricing()
        total = 0.0
        unpriced: list[str] = []
        for model, model_usage in self.by_model.items():
            # model-pricing.toml is only ever populated from opencode
            # zen's own pricing table (see file header) - bare model
            # names, no provider prefix - so strip a leading "opencode:"
            # before lookup. Other providers' prefixes (ollama:, copilot:)
            # are deliberately left alone: stripping those could collide
            # an unpriced model with an opencode entry that happens to
            # share the same bare name (see usage.add's own comment).
            rate = pricing.get(model.removeprefix("opencode:"))
            if rate is None:
                unpriced.append(model)
                continue
            total += (model_usage.prompt_tokens / 1_000_000) * rate["input_per_1m"]
            total += (model_usage.completion_tokens / 1_000_000) * rate["output_per_1m"]
        return total, unpriced

    def __str__(self) -> str:
        base = (
            f"{self.total_tokens} tokens total "
            f"({self.prompt_tokens} in / {self.completion_tokens} out)"
        )
        if not self.by_model:
            return base
        cost, unpriced = self.total_cost_usd()
        if not unpriced:
            return f"{base}, ~${cost:.4f}"
        if cost:
            return f"{base}, ~${cost:.4f}+ (no pricing for: {', '.join(unpriced)})"
        return f"{base}, cost unknown (no pricing for: {', '.join(unpriced)})"


# Process-lifetime accumulator. _post_chat_completion updates this on
# every response that includes a "usage" field; callers read it back
# (e.g. `print(ai_client.usage)`) whenever they want a running or final
# total - there's no per-call return value to thread through every
# run_prompt/run_with_tools call site for this.
usage = UsageTracker()

PRICING_FILE = Path(__file__).resolve().parent / "model-pricing.toml"
_pricing_cache: dict | None = None


def load_pricing() -> dict:
    """
    Loads ticket_pipeline/lib/model-pricing.toml: a [models."<model id>"] table per
    model with input_per_1m / output_per_1m USD rates. Missing file or
    missing model entries are not errors - cost just can't be computed
    for that model, which UsageTracker reports explicitly rather than
    guessing. Cached after first load since the file doesn't change
    mid-run.
    """
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    if not PRICING_FILE.exists():
        _pricing_cache = {}
        return _pricing_cache
    with PRICING_FILE.open("rb") as f:
        data = tomllib.load(f)
    _pricing_cache = data.get("models", {})
    return _pricing_cache


# opencode zen intermittently returns a bare HTTP 500 ("Unknown Error")
# or 502/503/504 with no useful body - observed to be transient gateway
# flakiness, not a real request problem (the same payload succeeds on a
# bare retry). 4xx errors (401 "No provider available", malformed
# request, etc.) are not retried - those are real, persistent failures
# that a retry won't fix.
RETRYABLE_HTTP_STATUSES = {500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_S = 2.0

# Default ceiling on tool-call turns within a single run_with_tools call -
# bounds a model that's merely confused (not stuck enough to call
# ask_user_prompt/run_command, just looping search_files/read_file with
# slightly different args turn after turn) rather than relying solely on
# those pseudo-tools to catch every runaway case. Generous enough for real
# multi-file exploration (see run_with_tools's own docstring reasoning),
# but not unbounded.
MAX_TURNS_PER_STEP = 40

# Optional process-wide cumulative cost ceiling, opt-in via env var rather
# than a hardcoded default: model-pricing.toml doesn't have an entry for
# every model (see UsageTracker.total_cost_usd's own `unpriced` list), so
# a default-on $ ceiling would silently fail to protect unpriced models
# while looking like it does. Unset means no cost ceiling (today's
# behavior).
MAX_COST_USD_ENV = "PIPELINE_MAX_COST_USD"


def _load_max_cost_usd() -> float | None:
    raw = os.environ.get(MAX_COST_USD_ENV)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise AIError(f"${MAX_COST_USD_ENV} must be a number, got {raw!r}")


# urllib's default User-Agent ("Python-urllib/3.x") trips Cloudflare's
# bot-fingerprint check (error 1010) on some endpoints - a normal-looking
# UA avoids that. Harmless for providers that don't care (e.g. a local
# Ollama server).
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Provider:
    """
    One OpenAI-compatible chat-completions backend. `name` is the
    "<name>:" prefix callers put on a model id to route to this provider
    (see resolve_provider) - never sent over the wire, stripped before
    the request.

    requires_api_key=False means no Authorization header is sent at all
    (e.g. a local Ollama server with no auth) - distinct from "key is
    optional", since some local setups genuinely have nothing to send
    and a missing-key error here would be wrong, not just inconvenient.

    auth_headers, when set, takes over auth entirely instead of the
    plain static-key Bearer header - for a provider whose auth isn't "one
    key, sent as-is" (see COPILOT: a cached GitHub OAuth token has to be
    exchanged for a short-lived session token first, plus extra required
    headers beyond Authorization). Called fresh on every request so it
    can refresh/cache internally; requires_api_key/api_key_file/
    api_key_env are ignored when this is set.
    """

    name: str
    base_url: str
    requires_api_key: bool = True
    api_key_file: Path | None = None
    api_key_env: str | None = None
    auth_headers: Callable[[], dict] | None = None

    def load_api_key(self) -> str:
        if self.api_key_file and self.api_key_file.exists():
            return self.api_key_file.read_text().strip()
        api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
        if not api_key:
            where = (
                f"{self.api_key_file} or ${self.api_key_env}"
                if self.api_key_file
                else f"${self.api_key_env}"
            )
            raise AIError(
                f"No API key found for provider '{self.name}' (checked {where})."
            )
        return api_key

    def request_headers(self) -> dict:
        if self.auth_headers is not None:
            return self.auth_headers()
        if self.requires_api_key:
            return {"Authorization": f"Bearer {self.load_api_key()}"}
        return {}


OPENCODE_ZEN = Provider(
    name="opencode",
    base_url=os.environ.get("OPENCODE_ZEN_BASE_URL", "https://opencode.ai/zen/v1"),
    requires_api_key=True,
    api_key_file=Path.home() / ".secrets" / "opencode-key",
    api_key_env="OPENCODE_ZEN_API_KEY",
)

# A local model server (`ollama serve`, default port 11434) exposing
# Ollama's built-in OpenAI-compatible endpoint - no API key needed since
# it's not a hosted service. Model ids are whatever's been pulled
# locally (`ollama list`), e.g. "ollama:llama3.1" or "ollama:qwen2.5-coder".
OLLAMA = Provider(
    name="ollama",
    base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    requires_api_key=False,
)

# GitHub Copilot's chat-completions backend isn't a plain static-key API:
# what's cached at COPILOT_OAUTH_TOKEN_FILE (via copilot_login.py's device
# flow - see that script) is a long-lived GitHub OAuth token, not the
# bearer this endpoint actually wants. That OAuth token has to be
# exchanged for a short-lived (~25min) Copilot session token via a
# separate endpoint, and the request also needs a couple of
# Copilot-specific headers beyond Authorization or the backend 401s -
# this is all undocumented-but-widely-relied-on behavior (the same
# approach copilot.vim/copilot.lua and various OSS Copilot proxies use),
# not an official API contract, so treat it as more likely to break on
# GitHub's end than the other providers here.
# Copilot is billed as a flat-rate subscription with a per-model premium-
# request multiplier (a request to an expensive model counts as more than
# 1 against the monthly quota), not $/token - model-pricing.toml's schema
# (input_per_1m/output_per_1m in USD) doesn't represent that at all.
# Deliberately not adding copilot:* entries there: a fabricated $/token
# number would be actively misleading, where "unpriced" (today's
# UsageTracker behavior for any model with no pricing entry) is at least
# honestly incomplete. Tracking premium-request *count* instead of $ cost
# would need a different mechanism than UsageTracker, not a pricing entry
# here - not implemented.
COPILOT_OAUTH_TOKEN_FILE = Path.home() / ".secrets" / "github-copilot-token"
COPILOT_OAUTH_TOKEN_ENV = "GITHUB_COPILOT_TOKEN"
COPILOT_TOKEN_EXCHANGE_URL = "https://api.github.com/copilot_internal/v2/token"
COPILOT_EDITOR_VERSION = "vscode/1.95.0"
COPILOT_PLUGIN_VERSION = "copilot-chat/0.23.0"

_copilot_session_token: str | None = None
_copilot_session_expires_at: float = 0.0
# A session token refresh mid-flight from two threads would just mean
# one extra redundant exchange call, not corruption - a lock here would
# be defense against a cost that doesn't exist, so this module-level
# cache is left unsynchronized deliberately.


def _load_copilot_oauth_token() -> str:
    if COPILOT_OAUTH_TOKEN_FILE.exists():
        return COPILOT_OAUTH_TOKEN_FILE.read_text().strip()
    token = os.environ.get(COPILOT_OAUTH_TOKEN_ENV)
    if token:
        return token
    raise AIError(
        f"No GitHub Copilot OAuth token at {COPILOT_OAUTH_TOKEN_FILE} and "
        f"${COPILOT_OAUTH_TOKEN_ENV} is not set. Run `python copilot_login.py` "
        f"once to authorize via your browser and cache the token."
    )


def _copilot_auth_headers() -> dict:
    global _copilot_session_token, _copilot_session_expires_at
    # 60s margin so a token that's about to expire isn't handed to a
    # request that then takes a few seconds to actually go out.
    if (
        _copilot_session_token is None
        or time.time() >= _copilot_session_expires_at - 60
    ):
        oauth_token = _load_copilot_oauth_token()
        req = urllib.request.Request(
            COPILOT_TOKEN_EXCHANGE_URL,
            headers={
                "Authorization": f"Bearer {oauth_token}",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                parsed = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            raise AIError(
                f"Copilot session token exchange failed: HTTP {e.code}: {error_body} - "
                f"the cached OAuth token may be expired or revoked; try "
                f"`python copilot_login.py` again."
            ) from e
        except urllib.error.URLError as e:
            raise AIError(f"Copilot session token exchange failed: {e.reason}") from e
        _copilot_session_token = parsed["token"]
        _copilot_session_expires_at = parsed.get("expires_at", time.time() + 1500)

    return {
        "Authorization": f"Bearer {_copilot_session_token}",
        "Editor-Version": COPILOT_EDITOR_VERSION,
        "Copilot-Integration-Id": "vscode-chat",
        "Editor-Plugin-Version": COPILOT_PLUGIN_VERSION,
    }


# A model id appearing in GET https://api.githubcopilot.com/models isn't
# a guarantee it works here - some (observed: gpt-5.4-mini) 400 with
# "unsupported_api_for_model" against /chat/completions, apparently
# routed through a different API shape (e.g. a Responses-style endpoint)
# that this provider doesn't implement. claude-sonnet-4.6/claude-haiku-4.5
# confirmed working as of 2026-06-25. Test a new model id directly before
# relying on it.
COPILOT = Provider(
    name="copilot",
    base_url="https://api.githubcopilot.com",
    auth_headers=_copilot_auth_headers,
)

# Keyed by the "<key>:" prefix callers use in a model id. "opencode:" is
# listed explicitly here (not just left as the implicit fallback) so it
# can be named the same way every other provider is - "opencode:gpt-5.4-mini"
# reads the same as "ollama:llama3.1" or "copilot:claude-sonnet-4.6". A
# bare model id with no recognized prefix (every existing caller, today)
# still falls through to DEFAULT_PROVIDER unchanged - adding "opencode:"
# here is additive, never changes what an existing unprefixed model id
# resolves to.
PROVIDERS: dict[str, Provider] = {
    "opencode": OPENCODE_ZEN,
    "ollama": OLLAMA,
    "copilot": COPILOT,
}
DEFAULT_PROVIDER = OPENCODE_ZEN
DEFAULT_MODEL = "opencode:default"


def resolve_provider(model: str) -> tuple[Provider, str]:
    """
    Splits a model id on its first ':' and checks the prefix against
    PROVIDERS. Returns (provider, model_id_without_prefix) - the
    provider never sees its own prefix, since that's purely this
    module's routing convention, not something the backend knows about.
    Unrecognized or absent prefixes fall through to DEFAULT_PROVIDER with
    the model id unchanged, so a bare "gpt-5.4-mini" or a model id that
    happens to contain ':' for some other reason both still work exactly
    as before this function existed.
    """
    prefix, sep, rest = model.partition(":")
    if sep and prefix in PROVIDERS:
        return PROVIDERS[prefix], rest
    return DEFAULT_PROVIDER, model


def _post_chat_completion(payload: dict, label: str) -> dict:
    original_model = payload["model"]
    provider, bare_model = resolve_provider(original_model)
    payload = {**payload, "model": bare_model}
    body = json.dumps(payload).encode()
    log.trace("%s request: %s", label, payload)

    attempt = 0
    while True:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            **provider.request_headers(),
        }
        req = urllib.request.Request(
            f"{provider.base_url}/chat/completions",
            data=body,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                parsed = json.loads(resp.read())
            log.trace("%s response: %s", label, parsed)
            break
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            if e.code not in RETRYABLE_HTTP_STATUSES or attempt >= MAX_RETRIES:
                raise AIError(
                    f"{label} request failed: HTTP {e.code}: {error_body}"
                ) from e
        except urllib.error.URLError as e:
            if attempt >= MAX_RETRIES:
                hint = (
                    f" (is the {provider.name} server running at {provider.base_url}?)"
                    if not provider.requires_api_key
                    else ""
                )
                raise AIError(f"{label} request failed: {e.reason}{hint}") from e

        attempt += 1
        backoff_s = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
        log.warning(
            "   %s: transient error, retrying in %.0fs (attempt %d/%d) ...",
            label,
            backoff_s,
            attempt,
            MAX_RETRIES,
        )
        time.sleep(backoff_s)

    response_usage = parsed.get("usage")
    if response_usage:
        # Tracked under the original (prefixed) model id, not the bare
        # one sent over the wire - so usage/cost reporting and
        # model-pricing.toml lookups stay keyed the same way callers
        # passed the model in, and an "ollama:llama3.1" run reports as
        # unpriced rather than colliding with an opencode model that
        # happens to share the same bare name.
        usage.add(original_model, response_usage)
    return parsed


def run_prompt(prompt: str, label: str, model: str = DEFAULT_MODEL) -> AIResult:
    """Send `prompt` to `model`'s provider (see resolve_provider). Raises AIError on failure."""
    provider, _ = resolve_provider(model)
    log.info("\n-- Running '%s' via %s (model=%s) ...", label, provider.base_url, model)
    parsed = _post_chat_completion(
        {"model": model, "messages": [{"role": "user", "content": prompt}]}, label
    )
    try:
        text = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise AIError(f"{label}: unexpected response shape: {parsed}") from e
    return AIResult(text=text)


def _default_summarize_call(name: str, args: dict) -> str:
    return f"{name}({args})"


def run_with_tools(
    prompt: str,
    tools: list[dict],
    executor: Callable[[str, dict], str],
    label: str,
    model: str = DEFAULT_MODEL,
    summarize_call: Callable[[str, dict], str] = _default_summarize_call,
    max_turns: int = MAX_TURNS_PER_STEP,
) -> AIResult:
    """
    Send `prompt` to opencode zen with `tools` available. Whenever the
    model's response includes tool_calls, run each through `executor`
    (signature: executor(tool_name, arguments_dict) -> str) and feed the
    result back as a tool message, then send another request - repeating
    until a response comes back with no tool_calls, which is treated as
    the final answer.

    `summarize_call` turns (name, args) into the one-line console log
    shown for each call - defaults to the raw name(args) form, but
    callers using tools.py should pass tools.summarize_tool_call so the
    log reads "Read foo.rs" instead of "read_file({'path': 'foo.rs'})".

    Bounded by two independent ceilings, both raising StepBudgetExceeded
    (a non-retryable AIError subclass - see pipeline_lib.run_ai_step_with_retry)
    rather than looping forever:
      - `max_turns`: the ask_user_prompt/run_command pseudo-tools (see
        tools.py) catch a model that knows it's stuck, but a model that's
        merely confused - never calling either, just looping
        search_files/read_file with slightly different args - has no
        other exit ramp. Each turn resends the *entire* message history,
        so this also bounds runaway cost growth, not just runaway time.
      - cumulative cost (see ai_client.usage / $PIPELINE_MAX_COST_USD):
        opt-in, process-wide, checked after every response that carries
        usage data.
    """
    max_cost_usd = _load_max_cost_usd()
    messages = [{"role": "user", "content": prompt}]

    provider, _ = resolve_provider(model)
    log.info("\n-- Running '%s' via %s (model=%s) ...", label, provider.base_url, model)
    turn = 0
    while True:
        turn += 1
        if turn > max_turns:
            msg = (
                f"{label}: exceeded {max_turns} turns with no final answer - aborting."
            )
            log.critical(msg)
            raise StepBudgetExceeded(msg)
        parsed = _post_chat_completion(
            {"model": model, "messages": messages, "tools": tools}, label
        )

        if max_cost_usd is not None:
            cost_so_far, _unpriced = usage.total_cost_usd()
            if cost_so_far >= max_cost_usd:
                msg = (
                    f"{label}: cumulative cost ~${cost_so_far:.4f} reached the "
                    f"${max_cost_usd:.4f} ceiling (${MAX_COST_USD_ENV}) - aborting."
                )
                log.critical(msg)
                raise StepBudgetExceeded(msg)

        try:
            message = parsed["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise AIError(f"{label}: unexpected response shape: {parsed}") from e

        messages.append(message)
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            return AIResult(text=message.get("content") or "")

        for call in tool_calls:
            function = call.get("function", {})
            name = function.get("name", "")
            try:
                args = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            log.debug("   %s", summarize_call(name, args))
            result_text = executor(name, args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result_text,
                }
            )
