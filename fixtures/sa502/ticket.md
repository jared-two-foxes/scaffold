# SA-502: Validate quote resend rate limit configuration

Support has seen a couple of incidents where a misconfigured environment
variable for a rate limit silently fell back to a default with no
warning, which made a misconfiguration hard to notice. We want quote
resending specifically to fail loudly at startup if its rate limit is
misconfigured, rather than silently using a bad value.

## Acceptance Criteria

- [ ] `QUOTE_RESEND_RATE_LIMIT` env var configures how many times a
      quote can be resent per period.
- [ ] A missing or empty value falls back to a sane default rather than
      erroring.
- [ ] A non-numeric or negative value produces a descriptive startup
      error naming the variable and the bad value, instead of silently
      falling back to the default.
- [ ] `cargo test -p virtual_assistant_api` passes with tests covering
      the above.
