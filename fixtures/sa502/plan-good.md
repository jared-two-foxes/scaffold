<!-- planned by Planner on 2026-06-25 from .ticket.md -->

## Source
Ticket: SA-502
Validate quote resend rate limit configuration

## Acceptance Criteria
- [ ] `QUOTE_RESEND_RATE_LIMIT` env var configures the resend limit
- [ ] Missing or empty value falls back to a sane default
- [ ] Non-numeric or negative value produces a descriptive startup error
- [ ] `cargo test -p virtual_assistant_api` passes with tests covering the above

## Implementation Plan
- No production code changes needed. `libs/virtual_assistant_api/src/infra/rate_limit_config.rs`
  already implements every criterion this ticket asks for:
  - `RateLimitConfig.quote_resend_rate_limit` is parsed from
    `QUOTE_RESEND_RATE_LIMIT` via the shared `parse_u32_or_default` helper.
  - Missing/empty falls back to `DEFAULT_QUOTE_RESEND_RATE_LIMIT` (5).
  - A non-numeric or negative value fails `trimmed.parse::<u32>()` and
    returns a descriptive error naming the variable and the bad value
    (negative values aren't valid `u32`, so they're rejected the same
    way as any other non-numeric string).
  - Existing tests (`parses_valid_rate_limit_values`,
    `uses_defaults_when_values_are_missing`,
    `uses_defaults_when_values_are_empty_or_whitespace`,
    `errors_on_non_numeric_string`, `errors_on_negative_number`) already
    cover this field's behavior via the shared parsing path.
  - Verify by running `cargo test -p virtual_assistant_api` and confirming
    these existing tests pass; no new test or implementation work is
    required for this ticket.
