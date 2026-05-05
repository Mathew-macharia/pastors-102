//! Minimal axum web UI for triggering buys and viewing status.
//!
//! Routes:
//!   GET  /              -- the HTML page (static/index.html)
//!   POST /api/arm       -- set the target {mint, creator, schedule?}
//!   POST /api/fire      -- manual fire (button)
//!   POST /api/cancel    -- cancel a pending schedule
//!   GET  /api/status    -- JSON status for the page to poll

use anyhow::Result;
use axum::{
    extract::{Json, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::sync::RwLock;
use tower_http::services::ServeDir;
use tracing::info;

use crate::trigger::{parse_scheduled, schedule, Trigger};

#[derive(Debug, Clone, Default, Serialize)]
pub struct UiState {
    pub mint: Option<String>,
    pub creator: Option<String>,
    pub scheduled_at_utc: Option<String>,
    pub status: String,
    pub last_buy_results: Option<serde_json::Value>,
    pub last_sell_results: Option<serde_json::Value>,
}

#[derive(Debug, Clone)]
pub struct AppState {
    pub trigger: Trigger,
    pub ui: Arc<RwLock<UiState>>,
}

#[derive(Debug, Deserialize)]
pub struct ArmRequest {
    pub mint: String,
    pub creator: String,
    /// Optional schedule: "YYYY-MM-DD HH:MM:SS" + tz like "America/New_York".
    /// If absent, only the manual fire button will trigger.
    pub schedule_local: Option<String>,
    pub schedule_tz: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ApiOk {
    pub ok: bool,
    pub message: String,
}

pub async fn run(addr: SocketAddr, state: AppState, static_dir: &str) -> Result<()> {
    info!("UI: listening on http://{}/", addr);

    let app = Router::new()
        .route("/api/arm", post(arm))
        .route("/api/fire", post(fire))
        .route("/api/cancel", post(cancel))
        .route("/api/status", get(status))
        .nest_service("/", ServeDir::new(static_dir))
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

async fn arm(State(s): State<AppState>, Json(req): Json<ArmRequest>) -> impl IntoResponse {
    {
        let mut ui = s.ui.write().await;
        ui.mint = Some(req.mint.clone());
        ui.creator = Some(req.creator.clone());
        ui.scheduled_at_utc = None;
        ui.status = "armed".into();
    }

    if let (Some(s_local), Some(tz)) = (req.schedule_local.as_deref(), req.schedule_tz.as_deref()) {
        match parse_scheduled(s_local, tz) {
            Ok(target_utc) => {
                {
                    let mut ui = s.ui.write().await;
                    ui.scheduled_at_utc = Some(target_utc.to_rfc3339());
                    ui.status = "scheduled".into();
                }
                let trig = s.trigger.clone();
                tokio::spawn(schedule(target_utc, trig));
            }
            Err(e) => {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(ApiOk { ok: false, message: format!("bad schedule: {e}") }),
                );
            }
        }
    }

    (
        StatusCode::OK,
        Json(ApiOk { ok: true, message: "armed".into() }),
    )
}

async fn fire(State(s): State<AppState>) -> impl IntoResponse {
    let ui_snap = s.ui.read().await.clone();
    if ui_snap.mint.is_none() || ui_snap.creator.is_none() {
        return (
            StatusCode::BAD_REQUEST,
            Json(ApiOk {
                ok: false,
                message: "not armed -- call /api/arm first".into(),
            }),
        );
    }
    s.trigger.fire_now();
    {
        let mut ui = s.ui.write().await;
        ui.status = "firing".into();
    }
    (
        StatusCode::OK,
        Json(ApiOk { ok: true, message: "fired".into() }),
    )
}

async fn cancel(State(s): State<AppState>) -> impl IntoResponse {
    // Note: this only clears the UI fields; it does NOT abort an in-flight
    // scheduled task. If you need hard abort, restart the binary.
    let mut ui = s.ui.write().await;
    ui.scheduled_at_utc = None;
    ui.status = "cancelled".into();
    (
        StatusCode::OK,
        Json(ApiOk { ok: true, message: "cancelled (UI state)".into() }),
    )
}

async fn status(State(s): State<AppState>) -> impl IntoResponse {
    let snap = s.ui.read().await.clone();
    Json(snap)
}
