# SA-452 — Add webhook config structs for Xero & QuickBooks verification secrets

| Field    | Value |
|----------|-------|
| State    | Todo |
| Priority | High |
| Assignee | Unassigned |
| Labels   | Feature |
| Created  | 2026-06-13 |
| Updated  | 2026-06-13 |
| URL      | https://linear.app/twofoxes/issue/SA-452/add-webhook-config-structs-for-xero-and-quickbooks-verification |

## Description

## Summary

Add Rocket-managed configuration structs for Xero and QuickBooks webhook verification secrets, parsed from environment variables. Follows the existing `StripeConfig` pattern in `infra/stripe.rs`.

## Files to Modify

* `libs/virtual_assistant_api/src/infra/accounting_webhooks.rs` — Xero & QuickBooks webhook config structs
* `libs/virtual_assistant_api/src/lib.rs` — Register Rocket state + manage config in build functions

## Requirements

### XeroWebhookConfig

* Debug output redacts `webhook_key` instead of printing the raw value.

### QuickbooksWebhookConfig

* Debug output redacts `webhook_token` instead of printing the raw value.

## Acceptance Criteria

- [ ] `Debug` output redacts the secret values
- [ ] A unit test proves the raw env value never appears in debug output
- [ ] `cargo test -p virtual_assistant_api` passes with the new config tests
- [ ] `cargo fmt` passes
- [ ] `cargo clippy -p virtual_assistant_api -- -D warnings` passes