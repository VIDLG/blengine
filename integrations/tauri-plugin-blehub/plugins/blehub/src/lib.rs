//! Tauri owns execution and platform assembly; BleHub remains a synchronous contract.

use std::sync::{Arc, Mutex};

use blehub::{BleBackend, BleEventSink, BleHub};
use tauri::{
    Emitter, Manager, RunEvent, Runtime,
    plugin::{Builder, TauriPlugin},
};

mod commands;
mod events;
pub mod projection;
#[cfg(test)]
mod tests;

#[cfg(all(any(target_os = "macos", target_os = "ios"), feature = "native"))]
mod apple_assembly;

#[derive(Clone)]
struct PluginState {
    hub: Arc<BleHub>,
    events: Arc<events::EventDelivery>,
}

impl PluginState {
    fn stop(
        &self,
    ) -> (
        Result<(), String>,
        Option<tauri::async_runtime::JoinHandle<()>>,
    ) {
        // Quiesce emission before draining native callbacks. No delivery lock
        // crosses Hub cleanup, which may wait for backend callback completion.
        let task = self.events.stop();
        (self.hub.dispose().map_err(|error| error.to_string()), task)
    }

    async fn close(&self) -> Result<(), String> {
        let state = self.clone();
        let (result, task) = tauri::async_runtime::spawn_blocking(move || state.stop())
            .await
            .map_err(|_| "Tauri BleHub cleanup task failed".to_owned())?;
        if let Some(task) = task {
            // stop() aborts the pump. Await its destruction as well as making
            // emission quiescent; buffered events are intentionally discarded.
            let _ = task.await;
        }
        result
    }
}

/// Installs the native provider owned by this plugin. Unsupported platforms
/// fail setup explicitly rather than silently falling back to a fake backend.
pub fn init<R: Runtime>() -> TauriPlugin<R> {
    plugin(assemble_native)
}

/// Injects a backend for deterministic tests or a caller-supplied provider.
/// This exposes exactly the same IPC and permissions as the native entry point.
pub fn with_backend<R: Runtime>(backend: Arc<dyn BleBackend>) -> TauriPlugin<R> {
    plugin(move |sink| BleHub::new(backend, sink).map_err(|error| error.to_string()))
}

fn assemble_native(sink: Arc<dyn BleEventSink>) -> Result<Arc<BleHub>, String> {
    #[cfg(all(any(target_os = "macos", target_os = "ios"), feature = "native"))]
    {
        let assembly = blehub::BackendAssembly::new(sink);
        apple_assembly::assemble(assembly.ticket())?;
        assembly.finish().map_err(|error| error.to_string())
    }
    #[cfg(all(target_os = "windows", feature = "native"))]
    {
        let backend = blehub_windows::WindowsBackend::new().map_err(|error| error.to_string())?;
        BleHub::new(Arc::new(backend), sink).map_err(|error| error.to_string())
    }
    #[cfg(not(all(
        feature = "native",
        any(target_os = "macos", target_os = "ios", target_os = "windows")
    )))]
    {
        drop(sink);
        Err("no native Tauri BleHub provider is enabled for this target".to_owned())
    }
}

fn plugin<R: Runtime>(
    assemble: impl FnOnce(Arc<dyn BleEventSink>) -> Result<Arc<BleHub>, String> + Send + 'static,
) -> TauriPlugin<R> {
    let owner: Arc<Mutex<Option<PluginState>>> = Arc::default();
    let setup_owner = owner.clone();
    let exit_owner = owner.clone();
    Builder::new("blehub")
        .invoke_handler(tauri::generate_handler![
            commands::capabilities,
            commands::open_settings,
            commands::initialize,
            commands::start_scan,
            commands::stop_scan,
            commands::connect,
            commands::disconnect,
            commands::discover_gatt,
            commands::read_characteristic,
            commands::write_characteristic,
            commands::set_notifications,
            commands::read_rssi,
            commands::close,
        ])
        .setup(move |app, _| {
            if app.try_state::<PluginState>().is_some() {
                return Err(
                    std::io::Error::other("BleHub is already installed in this app").into(),
                );
            }
            let (delivery, sink, receiver) = events::channel(128);
            let hub = assemble(sink).map_err(std::io::Error::other)?;
            let state = PluginState {
                hub,
                events: delivery.clone(),
            };
            if !app.manage(state.clone()) {
                let _ = state.stop();
                return Err(std::io::Error::other("BleHub state already exists").into());
            }
            *setup_owner
                .lock()
                .unwrap_or_else(|error| error.into_inner()) = Some(state.clone());
            let app = app.clone();
            let hub = Arc::downgrade(&state.hub);
            delivery.start(
                receiver,
                move |event| app.emit("blehub://event", event).is_ok(),
                move || {
                    if let Some(hub) = hub.upgrade() {
                        tauri::async_runtime::spawn_blocking(move || {
                            if hub.dispose().is_err() {
                                eprintln!("blehub_tauri_cleanup=failed; source=event_transport");
                            }
                        });
                    }
                },
            );
            Ok(())
        })
        .on_event(move |_, event| {
            // ExitRequested can be prevented by another plugin. Retire only
            // on actual Exit; closing one window is not closing the app.
            if matches!(event, RunEvent::Exit) {
                retire(&exit_owner);
            }
        })
        .on_drop(move |_| retire(&owner))
        .build()
}

fn retire(owner: &Mutex<Option<PluginState>>) {
    let state = owner
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .take();
    if let Some(state) = state {
        let (result, _) = state.stop();
        if result.is_err() {
            eprintln!("blehub_tauri_cleanup=failed; source=plugin_lifecycle");
        }
    }
}
