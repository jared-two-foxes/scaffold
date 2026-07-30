<!-- narrowed by Narrower on 2026-06-25 from .tdd-plan.md -->

## Source
Ticket: SA-500
Add a webhook retry rate limit to RateLimitConfig

## Acceptance Criteria
<!-- only criteria marked FAIL or UNKNOWN in Step 2 -->
- [ ] `WEBHOOK_RETRY_RATE_LIMIT` env var parsed into `RateLimitConfig.webhook_retry_rate_limit` <!-- why: no such field exists yet on RateLimitConfig -->
- [ ] Missing or empty env var defaults to `3` <!-- why: depends on the field existing first -->
- [ ] Non-numeric env var value produces a descriptive error naming the variable and the bad value <!-- why: depends on the field existing first -->
- [ ] `cargo test -p virtual_assistant_api` passes with new tests covering the above <!-- why: no tests exist yet for this field -->

## Implementation Plan
- `libs/virtual_assistant_api/src/infra/rate_limit_config.rs`: add `webhook_retry_rate_limit: u32` field to `RateLimitConfig`, a `DEFAULT_WEBHOOK_RETRY_RATE_LIMIT: u32 = 3` constant, parse it via the existing `parse_u32_or_default` helper keyed on `WEBHOOK_RETRY_RATE_LIMIT` (same as the other fields), update the `Default` impl, and add unit tests mirroring the existing ones (valid value, missing/empty defaults, non-numeric error, negative error)
