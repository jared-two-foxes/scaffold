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

* `libs/virtual_assistant_api/src/infra/mod.rs` — Add new modules
* `libs/virtual_assistant_api/src/infra/xero_webhook_config.rs` — NEW: Xero webhook config struct
* `libs/virtual_assistant_api/src/infra/quickbooks_webhook_config.rs` — NEW: QuickBooks webhook config struct
* `libs/virtual_assistant_api/src/notifications/email_config.rs` — Add new Rocket state fields
* `libs/virtual_assistant_api/src/lib.rs` — Register Rocket state + manage config in build functions

## Requirements

### XeroWebhookConfig

* Field: `webhook_key: String` — the Xero webhook signing key from the Xero developer portal
* Loaded from env var: `XERO_WEBHOOK_KEY`
* Struct derives `Debug` (redacts the key value like Postmark does)
* Optional integration into Rocket state via `Option<XeroWebhookConfig>` following `StripeConfig` pattern

### QuickbooksWebhookConfig

* Field: `webhook_token: String` — the Intuit webhook verification token
* Loaded from env var: `QUICKBOOKS_WEBHOOK_TOKEN`
* Same Debug redaction pattern
* Optional Rocket state via `Option<QuickbooksWebhookConfig>`

### Rocket State

* Both configs stored as `Option<T>` in Rocket state (like `StripeConfig`)
* Default to `None` when env vars are not set
* All existing `build_rocket` / `build_rocket_with_email_config` functions updated to include these (set to `None` in tests and noop mode)

## Acceptance Criteria

- [ ] `XERO_WEBHOOK_KEY` env var parsed into `XeroWebhookConfig` struct
- [ ] `QUICKBOOKS_WEBHOOK_TOKEN` env var parsed into `QuickbooksWebhookConfig` struct
- [ ] Both configs available as `Option<T>` Rocket state
- [ ] `Debug` output redacts the secret values
- [ ] Missing env vars produce `None` (no crash, graceful degradation)
- [ ] `cargo test -p virtual_assistant_api` passes with new config tests
- [ ] `cargo fmt` passes
- [ ] `cargo clippy -p virtual_assistant_api -- -D warnings` passes

## Edge Cases

* Both env vars are optional — app must start without them
* Empty string env var treated as unconfigured (None)
* Configs should be behind `#[cfg(not(test))]` or testable without env vars

## Complexity

`trivial` — ~80 lines of new code, follows established StripeConfig pattern exactly.
