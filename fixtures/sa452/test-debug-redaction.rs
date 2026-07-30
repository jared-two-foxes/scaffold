/// Xero webhook configuration loaded from environment variables.
#[derive(Clone, Debug)]
#[allow(dead_code)]
pub struct XeroWebhookConfig {
    pub webhook_key: String,
}

impl XeroWebhookConfig {
    /// Reads XERO_WEBHOOK_KEY from the environment.
    /// Returns None if the variable is missing or empty.
    #[allow(dead_code)]
    pub fn from_env() -> Option<Self> {
        let webhook_key = std::env::var("XERO_WEBHOOK_KEY")
            .ok()
            .filter(|s| !s.is_empty())?;
        Some(Self { webhook_key })
    }
}

/// QuickBooks webhook configuration loaded from environment variables.
#[derive(Clone, Debug)]
#[allow(dead_code)]
pub struct QuickbooksWebhookConfig {
    pub webhook_token: String,
}

impl QuickbooksWebhookConfig {
    /// Reads QUICKBOOKS_WEBHOOK_TOKEN from the environment.
    /// Returns None if the variable is missing or empty.
    #[allow(dead_code)]
    pub fn from_env() -> Option<Self> {
        let webhook_token = std::env::var("QUICKBOOKS_WEBHOOK_TOKEN")
            .ok()
            .filter(|s| !s.is_empty())?;
        Some(Self { webhook_token })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn debug_output_redacts_webhook_secrets() {
        let xero = XeroWebhookConfig {
            webhook_key: "xero-secret-token".to_string(),
        };
        let quickbooks = QuickbooksWebhookConfig {
            webhook_token: "quickbooks-secret-token".to_string(),
        };

        let xero_debug = format!("{xero:?}");
        let quickbooks_debug = format!("{quickbooks:?}");

        assert!(
            xero_debug.contains("[REDACTED]") && !xero_debug.contains("xero-secret-token"),
            "expected XeroWebhookConfig Debug to redact the secret; got: {xero_debug}"
        );
        assert!(
            quickbooks_debug.contains("[REDACTED]")
                && !quickbooks_debug.contains("quickbooks-secret-token"),
            "expected QuickbooksWebhookConfig Debug to redact the secret; got: {quickbooks_debug}"
        );
    }
}
