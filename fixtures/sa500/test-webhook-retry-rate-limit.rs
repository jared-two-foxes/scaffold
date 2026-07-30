//! Environment-backed configuration for API rate-limit thresholds.

use std::env;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RateLimitConfig {
    pub invite_rate_limit: u32,
    pub quote_send_rate_limit: u32,
    pub quote_resend_rate_limit: u32,
    pub login_rate_limit: u32,
    pub validate_invite_rate_limit: u32,
    pub accept_invite_rate_limit: u32,
}

impl Default for RateLimitConfig {
    fn default() -> Self {
        Self {
            invite_rate_limit: DEFAULT_INVITE_RATE_LIMIT,
            quote_send_rate_limit: DEFAULT_QUOTE_SEND_RATE_LIMIT,
            quote_resend_rate_limit: DEFAULT_QUOTE_RESEND_RATE_LIMIT,
            login_rate_limit: DEFAULT_LOGIN_RATE_LIMIT,
            validate_invite_rate_limit: DEFAULT_VALIDATE_INVITE_RATE_LIMIT,
            accept_invite_rate_limit: DEFAULT_ACCEPT_INVITE_RATE_LIMIT,
        }
    }
}

impl RateLimitConfig {
    pub fn from_env() -> Result<Self, String> {
        Self::from_lookup(|key| env::var(key).ok())
    }

    pub fn webhook_retry_rate_limit(&self) -> u32 {
        DEFAULT_WEBHOOK_RETRY_RATE_LIMIT
    }

    fn from_lookup<F>(lookup: F) -> Result<Self, String>
    where
        F: Fn(&str) -> Option<String>,
    {
        Ok(Self {
            invite_rate_limit: parse_u32_or_default(
                &lookup,
                "INVITE_RATE_LIMIT",
                DEFAULT_INVITE_RATE_LIMIT,
            )?,
            quote_send_rate_limit: parse_u32_or_default(
                &lookup,
                "QUOTE_SEND_RATE_LIMIT",
                DEFAULT_QUOTE_SEND_RATE_LIMIT,
            )?,
            quote_resend_rate_limit: parse_u32_or_default(
                &lookup,
                "QUOTE_RESEND_RATE_LIMIT",
                DEFAULT_QUOTE_RESEND_RATE_LIMIT,
            )?,
            login_rate_limit: parse_u32_or_default(
                &lookup,
                "LOGIN_RATE_LIMIT",
                DEFAULT_LOGIN_RATE_LIMIT,
            )?,
            validate_invite_rate_limit: parse_u32_or_default(
                &lookup,
                "VALIDATE_INVITE_RATE_LIMIT",
                DEFAULT_VALIDATE_INVITE_RATE_LIMIT,
            )?,
            accept_invite_rate_limit: parse_u32_or_default(
                &lookup,
                "ACCEPT_INVITE_RATE_LIMIT",
                DEFAULT_ACCEPT_INVITE_RATE_LIMIT,
            )?,
        })
    }
}

const DEFAULT_INVITE_RATE_LIMIT: u32 = 5;
const DEFAULT_QUOTE_SEND_RATE_LIMIT: u32 = 5;
const DEFAULT_QUOTE_RESEND_RATE_LIMIT: u32 = 5;
const DEFAULT_LOGIN_RATE_LIMIT: u32 = 10;
const DEFAULT_VALIDATE_INVITE_RATE_LIMIT: u32 = 10;
const DEFAULT_ACCEPT_INVITE_RATE_LIMIT: u32 = 10;
const DEFAULT_WEBHOOK_RETRY_RATE_LIMIT: u32 = 3;

fn parse_u32_or_default<F>(lookup: &F, key: &str, default: u32) -> Result<u32, String>
where
    F: Fn(&str) -> Option<String>,
{
    match lookup(key) {
        Some(raw_value) => {
            let trimmed = raw_value.trim();
            if trimmed.is_empty() {
                return Ok(default);
            }

            trimmed.parse::<u32>().map_err(|_| {
                format!(
                    "invalid environment variable {key} with value '{raw_value}': expected unsigned 32-bit integer"
                )
            })
        }
        None => Ok(default),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn map_lookup<'a>(
        vars: &'a HashMap<&'static str, &'static str>,
    ) -> impl Fn(&str) -> Option<String> + 'a {
        move |key| vars.get(key).map(|value| (*value).to_string())
    }

    #[test]
    fn parses_valid_rate_limit_values() {
        let mut vars = HashMap::new();
        vars.insert("INVITE_RATE_LIMIT", " 12 ");
        vars.insert("QUOTE_SEND_RATE_LIMIT", "34");
        vars.insert("QUOTE_RESEND_RATE_LIMIT", "56");
        vars.insert("LOGIN_RATE_LIMIT", "7");

        let config = RateLimitConfig::from_lookup(map_lookup(&vars)).expect("expected config");
        assert_eq!(config.invite_rate_limit, 12);
        assert_eq!(config.quote_send_rate_limit, 34);
        assert_eq!(config.quote_resend_rate_limit, 56);
        assert_eq!(config.login_rate_limit, 7);
    }

    #[test]
    fn parses_webhook_retry_rate_limit_value() {
        let mut vars = HashMap::new();
        vars.insert("WEBHOOK_RETRY_RATE_LIMIT", "9");

        let config = RateLimitConfig::from_lookup(map_lookup(&vars)).expect("expected config");
        assert_eq!(config.webhook_retry_rate_limit(), 9);
    }

    #[test]
    fn uses_defaults_when_values_are_missing() {
        let vars = HashMap::new();

        let config = RateLimitConfig::from_lookup(map_lookup(&vars)).expect("expected config");
        assert_eq!(config.invite_rate_limit, DEFAULT_INVITE_RATE_LIMIT);
        assert_eq!(config.quote_send_rate_limit, DEFAULT_QUOTE_SEND_RATE_LIMIT);
        assert_eq!(
            config.quote_resend_rate_limit,
            DEFAULT_QUOTE_RESEND_RATE_LIMIT
        );
        assert_eq!(config.login_rate_limit, DEFAULT_LOGIN_RATE_LIMIT);
    }

    #[test]
    fn uses_defaults_when_values_are_empty_or_whitespace() {
        let mut vars = HashMap::new();
        vars.insert("INVITE_RATE_LIMIT", "");
        vars.insert("QUOTE_SEND_RATE_LIMIT", "   ");
        vars.insert("QUOTE_RESEND_RATE_LIMIT", "\t\n");
        vars.insert("LOGIN_RATE_LIMIT", "");

        let config = RateLimitConfig::from_lookup(map_lookup(&vars)).expect("expected config");
        assert_eq!(config.invite_rate_limit, DEFAULT_INVITE_RATE_LIMIT);
        assert_eq!(config.quote_send_rate_limit, DEFAULT_QUOTE_SEND_RATE_LIMIT);
        assert_eq!(
            config.quote_resend_rate_limit,
            DEFAULT_QUOTE_RESEND_RATE_LIMIT
        );
        assert_eq!(config.login_rate_limit, DEFAULT_LOGIN_RATE_LIMIT);
    }

    #[test]
    fn errors_on_non_numeric_string() {
        let mut vars = HashMap::new();
        vars.insert("INVITE_RATE_LIMIT", "abc");

        let error =
            RateLimitConfig::from_lookup(map_lookup(&vars)).expect_err("expected parse error");
        assert!(error.contains("INVITE_RATE_LIMIT"));
        assert!(error.contains("abc"));
    }

    #[test]
    fn errors_on_negative_number() {
        let mut vars = HashMap::new();
        vars.insert("QUOTE_SEND_RATE_LIMIT", "-1");

        let error =
            RateLimitConfig::from_lookup(map_lookup(&vars)).expect_err("expected parse error");
        assert!(error.contains("QUOTE_SEND_RATE_LIMIT"));
        assert!(error.contains("-1"));
    }

    #[test]
    fn errors_on_float_value() {
        let mut vars = HashMap::new();
        vars.insert("QUOTE_RESEND_RATE_LIMIT", "1.5");

        let error =
            RateLimitConfig::from_lookup(map_lookup(&vars)).expect_err("expected parse error");
        assert!(error.contains("QUOTE_RESEND_RATE_LIMIT"));
        assert!(error.contains("1.5"));
    }

    #[test]
    fn errors_on_overflow_value() {
        let mut vars = HashMap::new();
        vars.insert("INVITE_RATE_LIMIT", "4294967296");

        let error =
            RateLimitConfig::from_lookup(map_lookup(&vars)).expect_err("expected parse error");
        assert!(error.contains("INVITE_RATE_LIMIT"));
        assert!(error.contains("4294967296"));
    }
}
