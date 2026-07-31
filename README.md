# Scaffold

Scaffold is a local developer workflow tool for working through Linear tickets in a test-driven, criteria-driven loop. It helps you fetch a ticket, build a plan, narrow scope, write tests, implement changes, and review the results without having to remember a long list of individual commands.

The project is organized around two layers:

- Core workflow code in the package under src/ticket_pipeline
- A separate CLI entrypoint in src/scaffold_cli for the command dispatcher and user-facing command routing

## Features

- Fetch and inspect Linear tickets
- Build and refine a plan for a ticket
- Advance through a criteria-based workflow step by step
- Review tickets against the current codebase
- Reset pipeline state or individual criteria when needed
- Run targeted benchmarking helpers for development and debugging

## Requirements

- Python 3.11+
- An editable install of this project

## Installation

From the repository root:

```bash
pip install -e .
```

This project is designed to be used from a checked-out source tree, so editable installs are the recommended setup.

## Usage

The main entrypoint is the `scaffold` command:

```bash
scaffold --help
```

Common commands include:

```bash
scaffold status
scaffold push-ticket
scaffold next-step
scaffold review-ticket
scaffold list-models
```

To see the options for a specific subcommand:

```bash
scaffold <command> --help
```

## Development

Run the test suite with:

```bash
pytest
```

You can also run a targeted test file:

```bash
pytest -q tests/test_cli_entrypoint.py
```

### Build a standalone binary

This project can be packaged into a single executable with a single command:

```bash
python build_binary.py
```

The script uses PyInstaller under the hood and writes the resulting binary to the dist directory.

## Project Layout

```text
src/
  scaffold_cli/      # CLI entrypoint and dispatcher
  ticket_pipeline/   # Core workflow implementation
tests/               # Project tests
fixtures/            # Fixture data used by tests and benchmarks
```

## Notes

The CLI wrapper is intentionally separated from the workflow engine so the core pipeline can later be reused by other interfaces such as a daemon or service without coupling everything to command-line conventions.
