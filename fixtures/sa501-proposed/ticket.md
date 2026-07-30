# SA-501: Wire Postmark signing secret into config

Postmark signs each webhook request; the signing secret needs to be available in `EmailConfig` so the existing webhook verification path can use it. Clarify whether `POSTMARK_SIGNING_SECRET` replaces `postmark_webhook_token` or is accepted as an alias for it.

## Acceptance Criteria

- [ ] `POSTMARK_SIGNING_SECRET` env var is parsed into `EmailConfig.postmark_signing_secret: Option<String>`, following the existing pattern used for `postmark_server_token` and `postmark_webhook_token`.
- [ ] A missing or empty `POSTMARK_SIGNING_SECRET` results in `None`, same as the other optional Postmark fields.
- [ ] `EmailConfig` debug output does not expose `postmark_signing_secret`; it is redacted or omitted the same way as the existing Postmark secrets, with test coverage.
- [ ] `cargo test -p virtual_assistant_api` passes with new tests covering the above.