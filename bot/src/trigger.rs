//! Trigger sources: a UI button or a scheduled time.
//!
//! Both feed into a `tokio::sync::oneshot` (or broadcast for cancel).
//! The UI calls `fire_now()` on its handle; a scheduled time waits then fires.

use anyhow::Result;
use chrono::{DateTime, TimeZone, Utc};
use chrono_tz::Tz;
use std::sync::Arc;
use tokio::sync::Notify;
use tracing::info;

/// Trigger handle shared between the UI and the firing task.
#[derive(Debug, Clone)]
pub struct Trigger {
    notify: Arc<Notify>,
}

impl Default for Trigger {
    fn default() -> Self {
        Self::new()
    }
}

impl Trigger {
    pub fn new() -> Self {
        Self {
            notify: Arc::new(Notify::new()),
        }
    }

    pub fn fire_now(&self) {
        info!("trigger: fire_now()");
        self.notify.notify_one();
    }

    pub async fn wait(&self) {
        self.notify.notified().await;
    }
}

/// Parse a "YYYY-MM-DD HH:MM:SS" + IANA timezone string into a UTC instant.
pub fn parse_scheduled(s: &str, tz: &str) -> Result<DateTime<Utc>> {
    let zone: Tz = tz.parse().map_err(|e| anyhow::anyhow!("bad tz {}: {}", tz, e))?;
    let naive = chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S")
        .map_err(|e| anyhow::anyhow!("parse '{}': {}", s, e))?;
    let local = zone
        .from_local_datetime(&naive)
        .single()
        .ok_or_else(|| anyhow::anyhow!("ambiguous local time {} in {}", s, tz))?;
    Ok(local.with_timezone(&Utc))
}

/// Sleep until `target` UTC, then fire the trigger.
pub async fn schedule(target: DateTime<Utc>, trig: Trigger) {
    let now = Utc::now();
    let diff = target - now;
    let secs = diff.num_seconds().max(0);
    info!("trigger: scheduled at {} ({} s from now)", target, secs);
    if secs > 0 {
        tokio::time::sleep(std::time::Duration::from_secs(secs as u64)).await;
    }
    // Fine sleep for sub-second accuracy
    let now2 = Utc::now();
    let frac = (target - now2).num_milliseconds().max(0);
    if frac > 0 {
        tokio::time::sleep(std::time::Duration::from_millis(frac as u64)).await;
    }
    trig.fire_now();
}
