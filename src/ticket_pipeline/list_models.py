#!/usr/bin/env python3
"""
list-models - list the models available from a configured ai_client.py
provider (opencode/ollama/copilot), by calling that provider's own
GET /models endpoint with the same auth ai_client.py uses for real
requests.

Useful before relying on a new model id in any of the other scripts:
appearing in this list is necessary but not sufficient - ai_client.py's
own COPILOT comment notes at least one model (gpt-5.4-mini) that's listed
by Copilot's /models but 400s against /chat/completions with
"unsupported_api_for_model". Test a model id directly (e.g. a one-off
review-ticket.py/bench_block.py call) before trusting it just because it
showed up here.

Usage:
    list-models [--provider opencode|ollama|copilot] [--raw]
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

from .lib import ai_client, render


def fetch_models(provider: ai_client.Provider) -> list[dict]:
    headers = {**provider.request_headers(), "User-Agent": ai_client._USER_AGENT}
    req = urllib.request.Request(f"{provider.base_url}/models", headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise ai_client.AIError(
            f"GET {provider.base_url}/models failed: HTTP {e.code}: {e.read().decode(errors='replace')}"
        ) from e
    except urllib.error.URLError as e:
        raise ai_client.AIError(
            f"GET {provider.base_url}/models failed: {e} "
            f"(is the {provider.name} server running at {provider.base_url}?)"
        ) from e
    items = data.get("data", data if isinstance(data, list) else [])
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--provider",
        default="opencode",
        choices=sorted(ai_client.PROVIDERS),
        help="Which provider's /models endpoint to query (default: opencode).",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print the full JSON for each model instead of just its id "
             "(and vendor/owned_by, if present).",
    )
    args = parser.parse_args()

    provider = ai_client.PROVIDERS[args.provider]
    try:
        models = fetch_models(provider)
    except ai_client.AIError as e:
        render.print_line(f"error: {e}")
        sys.exit(1)

    render.print_line(f"-- {len(models)} model(s) available from '{args.provider}' ({provider.base_url}):")
    render.print_line()
    if args.raw:
        render.print_line(json.dumps(models, indent=2))
        return

    for model in sorted(models, key=lambda m: m.get("id", "")):
        model_id = model.get("id", "(no id)")
        extra = model.get("vendor") or model.get("owned_by")
        render.print_line(f"   {model_id}" + (f"  ({extra})" if extra else ""))


if __name__ == "__main__":
    main()
