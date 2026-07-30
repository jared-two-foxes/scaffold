# SA-500 — Add a webhook retry rate limit to RateLimitConfig

| Field    | Value |
|----------|-------|
| State    | Todo |
| Priority | Medium |
| Assignee | Unassigned |
| Labels   | Feature |
| Created  | 2026-06-25 |
| Updated  | 2026-06-25 |
| URL      | https://linear.app/twofoxes/issue/SA-500/add-webhook-retry-rate-limit |

## Description

## Summary

Add a `webhook_retry_rate_limit` field to `RateLimitConfig`
(`infra/rate_limit_config.rs`), following the exact same pattern as the
existing fields (`invite_rate_limit`, `quote_send_rate_limit`, etc.):
sourced from an environment variable, with a documented default, parsed
through the existing `parse_u32_or_default` helper.

## Requirements

* Field: `webhook_retry_rate_limit: u32`
* Loaded from env var: `WEBHOOK_RETRY_RATE_LIMIT`
* Default: `3` (if missing or empty)
* Non-numeric values produce a descriptive error, same as the existing
  fields (see `errors_on_non_numeric_string` etc. in the existing test
  module for the expected error shape)

## Acceptance Criteria

- [ ] `WEBHOOK_RETRY_RATE_LIMIT` env var parsed into `RateLimitConfig.webhook_retry_rate_limit`
- [ ] Missing or empty env var defaults to `3`
- [ ] Non-numeric env var value produces a descriptive error naming the variable and the bad value
- [ ] `cargo test -p virtual_assistant_api` passes with new tests covering the above

## Edge Cases

* Whitespace-only env var value is treated the same as missing (defaults to `3`)
* Negative or overflowing values are rejected with the same error pattern as the existing fields

## Complexity

`trivial` — single new field on an existing struct, following an
established pattern exactly; no new files, no ambiguity about where the
work belongs.
