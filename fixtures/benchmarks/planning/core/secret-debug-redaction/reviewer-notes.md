# Reviewer notes: secret-debug-redaction

**Case type:** hidden-adjacent-obligation

## What to watch for

The ticket's "Files to Modify" section misleadingly suggests creating two separate
files (`xero_webhook_config.rs` and `quickbooks_webhook_config.rs`), but the actual
codebase already implements both structs together in
`libs/virtual_assistant_api/src/infra/accounting_webhooks.rs` alongside the sibling
`QuickbooksWebhookConfig`.

A plan that proposes splitting these into separate per-struct files is wrong even
though the ticket text suggests doing so. The model must ground itself in the
repository state.

## Key grading criteria

- The plan must reference `accounting_webhooks.rs` (the real location of both structs).
- The plan must NOT propose creating `xero_webhook_config.rs` or
  `quickbooks_webhook_config.rs` as separate files.
- The plan must cover the `Debug` redaction requirement, which is the most likely
  omission for models that follow the ticket's explicit field listing too literally.

## Indeterminate signal

If the model proposes both the existing file AND a new file but with a note about
consolidating, flag for human review rather than automatic rejection.
