#!/usr/bin/env python3
"""
Fetch a Linear ticket by identifier and print the rendered markdown to
stdout, e.g. python -m ticket_pipeline.lib.fetch_ticket SA-456

fetch_ticket() and render() are plain functions with no file I/O.
This file's __main__ block is just a thin CLI wrapper around the same
two functions, for manual/standalone use.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_api_key() -> str:
    key_file = Path.home() / ".secrets" / "linear-key"
    return key_file.read_text().strip()


def _graphql(query: str, variables: dict) -> dict:
    """Shared POST-and-parse for every Linear GraphQL call in this module -
    query and mutation alike, since both are just a query string + variables
    over the same endpoint with the same auth header."""
    api_key = load_api_key()
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def fetch_ticket(identifier: str) -> dict:
    query = """
    query Issue($identifier: String!) {
      issue(id: $identifier) {
        id
        identifier
        title
        description
        priority
        state { name }
        assignee { name email }
        labels { nodes { name } }
        team { id }
        createdAt
        updatedAt
        url
      }
    }
    """
    return _graphql(query, {"identifier": identifier})


PRIORITY_LABELS = {0: "No priority", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def render(data: dict) -> str:
    if "errors" in data:
        for e in data["errors"]:
            print(f"Error: {e['message']}", file=sys.stderr)
        sys.exit(1)

    issue = data.get("data", {}).get("issue")
    if not issue:
        print("Ticket not found.", file=sys.stderr)
        sys.exit(1)

    labels = ", ".join(n["name"] for n in issue["labels"]["nodes"]) or "—"
    assignee = issue["assignee"]["name"] if issue["assignee"] else "Unassigned"
    priority = PRIORITY_LABELS.get(issue["priority"], str(issue["priority"]))

    lines = [
        f"# {issue['identifier']} — {issue['title']}",
        "",
        "| Field    | Value |",
        "|----------|-------|",
        f"| State    | {issue['state']['name']} |",
        f"| Priority | {priority} |",
        f"| Assignee | {assignee} |",
        f"| Labels   | {labels} |",
        f"| Created  | {issue['createdAt'][:10]} |",
        f"| Updated  | {issue['updatedAt'][:10]} |",
        f"| URL      | {issue['url']} |",
    ]
    if issue["description"]:
        lines += ["", "## Description", "", issue["description"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(f"Usage: {sys.argv[0]} <ticket-id>  (e.g. SA-456)", file=sys.stderr)
        sys.exit(0)
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <ticket-id>  (e.g. SA-456)", file=sys.stderr)
        sys.exit(1)
    identifier = sys.argv[1]
    try:
        data = fetch_ticket(identifier)
        content = render(data)
        sys.stdout.write(content)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
