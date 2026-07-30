#!/usr/bin/env python3
"""
Fetch a Linear ticket by identifier and print the rendered markdown to
stdout, e.g. ./fetch_ticket.py SA-456

fetch_ticket() and render() are plain functions with no file I/O -
check-ticket.py and tdd-pipeline.py import this module directly and
call them rather than subprocessing this file, so the ticket content
never touches disk except via tools.write_file_block in those scripts.
This file's __main__ block is just a thin CLI wrapper around the same
two functions, for manual/standalone use.
"""

import sys
import json
import urllib.request
import urllib.error
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


def update_ticket(issue_id: str, title: str | None = None, description: str | None = None) -> dict:
    """
    Mutates the title/description of an existing Linear issue. issue_id
    is the issue's internal UUID (the "id" field fetch_ticket() returns,
    not its human-readable identifier like "SA-42" - Linear's
    issueUpdate mutation takes the former). Only used by update-ticket.py
    - this is the one write path against Linear in this whole set of
    scripts, deliberately not called from anywhere else.
    """
    mutation = """
    mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
      issueUpdate(id: $id, input: $input) {
        success
        issue { id identifier title updatedAt }
      }
    }
    """
    input_fields = {}
    if title is not None:
        input_fields["title"] = title
    if description is not None:
        input_fields["description"] = description
    return _graphql(mutation, {"id": issue_id, "input": input_fields})


def create_ticket(team_id: str, title: str, description: str, parent_id: str | None = None) -> dict:
    """
    Creates a new Linear issue via the issueCreate mutation. team_id is
    the internal UUID of the team the new issue belongs to (fetch_ticket()'s
    "team.id" field on the parent, for a sub-issue). parent_id, if given,
    is the parent issue's internal UUID (not its human-readable identifier)
    - links the new issue as a Linear sub-issue rather than a standalone one.
    Only used by create_child_tickets.py - the other write path against
    Linear in this set of scripts, alongside update_ticket() above.
    """
    mutation = """
    mutation IssueCreate($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue { id identifier title url }
      }
    }
    """
    input_fields = {"teamId": team_id, "title": title, "description": description}
    if parent_id is not None:
        input_fields["parentId"] = parent_id
    return _graphql(mutation, {"input": input_fields})


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
        f"| Field    | Value |",
        f"|----------|-------|",
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
