# SA-501: Verify Postmark webhook signatures

We currently accept Postmark inbound webhook calls without verifying
they actually came from Postmark. We need to add signature
verification.

Postmark signs each webhook request; the signing secret needs to live
alongside our other Postmark credentials so the webhook handler can
verify the signature before processing the payload.

## Acceptance Criteria

- [ ] `POSTMARK_SIGNING_SECRET` env var is parsed into
      `EmailConfig.postmark_signing_secret: Option<String>`, following
      the existing pattern used for `postmark_server_token` and
      `postmark_webhook_token`.
- [ ] A missing or empty `POSTMARK_SIGNING_SECRET` results in `None`,
      same as the other optional Postmark fields.
- [ ] `cargo test -p virtual_assistant_api` passes with new tests
      covering the above.

Signature verification itself (computing and checking the HMAC against
incoming webhook bodies) is a separate follow-up ticket - this ticket is
just about getting the secret safely into config.
