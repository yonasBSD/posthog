use std::collections::HashMap;

use anyhow::Error;
use chrono::{DateTime, Utc};
use common_types::{CapturedEvent, InternallyCapturedEvent, RawEvent};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use super::TransformContext;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct MixpanelContentConfig {
    #[serde(default)] // Defaults to false
    pub skip_no_distinct_id: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct MixpanelEvent {
    event: String,
    properties: MixpanelProperties,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct MixpanelProperties {
    #[serde(rename = "time")]
    timestamp_ms: i64,
    distinct_id: Option<String>,
    #[serde(flatten)]
    other: HashMap<String, Value>,
}

// Based off sample data provided by customer.
impl MixpanelEvent {
    pub fn parse_fn(
        context: TransformContext,
        skip_no_distinct_id: bool,
        event_transform: impl Fn(RawEvent) -> Result<Option<RawEvent>, Error>,
    ) -> impl Fn(Self) -> Result<Option<InternallyCapturedEvent>, Error> {
        move |mx| {
            let token = context.token.clone();
            let team_id = context.team_id;

            let distinct_id = match (
                mx.properties.distinct_id.as_ref().cloned(),
                skip_no_distinct_id,
            ) {
                (Some(distinct_id), _) => distinct_id,
                (None, true) => return Ok(None),
                (None, false) => Uuid::now_v7().to_string(),
            };

            let generated_id = Uuid::now_v7();
            // We don't support subsecond precision for historical imports
            let timestamp = DateTime::<Utc>::from_timestamp(mx.properties.timestamp_ms / 1000, 0)
                .ok_or(Error::msg("Invalid timestamp"))?;

            let raw_event = RawEvent {
                token: Some(token.clone()),
                distinct_id: Some(Value::String(distinct_id.clone())),
                uuid: Some(generated_id),
                event: map_event_names(mx.event),
                properties: mx.properties.other,
                // We send timestamps in iso 1806 format
                timestamp: Some(timestamp.to_rfc3339()),
                set: None,
                set_once: None,
                offset: None,
            };

            let Some(raw_event) = event_transform(raw_event)? else {
                return Ok(None);
            };

            let inner = CapturedEvent {
                uuid: generated_id,
                distinct_id,
                ip: "127.0.0.1".to_string(),
                data: serde_json::to_string(&raw_event)?,
                now: Utc::now().to_rfc3339(),
                sent_at: None,
                token,
                is_cookieless_mode: false,
            };

            Ok(Some(InternallyCapturedEvent { team_id, inner }))
        }
    }
}

// Maps mixpanel event names to posthog event names
pub fn map_event_names(event: String) -> String {
    // TODO - add more as you find them
    match event.as_str() {
        "$mp_web_page_view" => "$pageview".to_string(),
        _ => event,
    }
}
