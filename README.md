# Scaffold

Scaffold is a ticket-management CLI focused only on:

- Accessing Linear tickets
- Maintaining the criteria stack
- Performing stack operations (push, pop, list, clear)

## Installation

```bash
pip install -e .
```

## Usage

```bash
scaffold --help
scaffold fetch-ticket SA-123
scaffold status
scaffold stack list
scaffold stack push --ticket SA-123 --criterion "- [ ] Add API field"
scaffold stack pop
scaffold stack clear
```
