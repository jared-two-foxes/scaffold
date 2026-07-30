<!-- narrowed by Narrower on 2026-06-13 from .tdd-plan.md -->

## Source
Ticket: SA-452
Add webhook config structs for Xero & QuickBooks verification secrets

## Acceptance Criteria
<!-- only criteria marked FAIL or UNKNOWN in Step 2 -->
- [ ] `Debug` output redacts the secret values <!-- why: both structs use bare `#[derive(Debug)]` which prints the secret; no custom redacting fmt::Debug impl exists (only EmailConfig has one) -->
- [ ] `cargo fmt` passes <!-- why: no `cargo fmt` command output was provided as evidence, so it can't be confirmed -->
- [ ] `cargo clippy -p virtual_assistant_api -- -D warnings` passes <!-- why: no `cargo clippy` command output was provided as evidence, so it can't be confirmed -->

## Implementation Plan
- `infra/accounting_webhooks.rs`: Replace bare `#[derive(Debug)]` on both `XeroWebhookConfig` and `QuickbooksWebhookConfig` with manual `fmt::Debug` impls that redact the secret value (following the `EmailConfig` pattern in `notifications/email_config.rs`)
