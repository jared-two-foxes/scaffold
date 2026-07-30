<!-- planned by Planner on 2026-06-25 from .ticket.md -->

## Source
Ticket: SA-501
Verify Postmark webhook signatures

## Acceptance Criteria
- [ ] `POSTMARK_SIGNING_SECRET` env var parsed into `EmailConfig.postmark_signing_secret`
- [ ] Missing or empty env var results in `None`
- [ ] The new field never appears in plaintext in Debug output or logs
- [ ] `cargo test -p virtual_assistant_api` passes with new tests covering the above

## Implementation Plan
- `libs/virtual_assistant_api/src/notifications/email_config.rs`:
  - Add `postmark_signing_secret: Option<String>` to `EmailConfig`, parsed
    in `from_lookup` the same way `postmark_server_token`/
    `postmark_webhook_token` are (via `lookup("POSTMARK_SIGNING_SECRET")`,
    trimmed, empty/missing -> `None`).
  - `EmailConfig`'s `Debug` impl is hand-written (not `#[derive(Debug)]`),
    so it must be updated explicitly: add a
    `.field("postmark_signing_secret", &self.postmark_signing_secret.as_ref().map(|_| "[REDACTED]"))`
    line alongside the existing `postmark_server_token`/
    `postmark_webhook_token` redaction lines - otherwise the new field
    would print in cleartext, since nothing else redacts it
    automatically.
  - Add unit tests mirroring the existing ones for the other optional
    Postmark fields (value present, missing/empty -> `None`).
