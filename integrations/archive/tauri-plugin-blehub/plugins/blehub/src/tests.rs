//! Plugin setup/drop integration through Tauri's official mock app APIs.
//!
//! No windows or event loop are started: MockRuntime::run_iteration is a no-op,
//! and run/run_return enter its window-driven loop. These tests do not cover
//! RunEvent::Exit/ExitRequested or native app shutdown. Command functions use
//! real managed State, but this is not frontend IPC/ACL coverage.

use std::{
    future::Future,
    sync::{Arc, Weak, mpsc},
    time::Duration,
};

use blehub::{BleEvent, CommandId, EventIngressStatus, InitializeCommand};
use blehub_testkit::{EventScenario, FakeBackend, FakeBackendHandle, RecordedCommand};
use tauri::{
    App, Manager,
    test::{MockRuntime, mock_builder, mock_context, noop_assets},
};

use super::{PluginState, commands, projection::WireId, with_backend};

fn mock_plugin_app(
    scenarios: impl IntoIterator<Item = EventScenario>,
) -> (App<MockRuntime>, FakeBackendHandle, Weak<FakeBackend>) {
    let (backend, handle) = FakeBackend::scripted(scenarios);
    let backend = Arc::new(backend);
    let owner = Arc::downgrade(&backend);
    let app = mock_builder()
        .plugin(with_backend(backend))
        .build(mock_context(noop_assets()))
        .expect("build the mock app and initialize BleHub");
    assert!(handle.ingress_for_callback().is_some());
    assert_eq!(handle.dispose_count(), 0);
    (app, handle, owner)
}

// Bound async cleanup without a Tokio dev-dependency or a window event loop.
#[track_caller]
fn await_cleanup(future: impl Future<Output = ()> + Send + 'static) {
    let (tx, rx) = mpsc::channel();
    let task = tauri::async_runtime::spawn(async move {
        future.await;
        let _ = tx.send(());
    });
    let result = rx.recv_timeout(Duration::from_secs(10));
    if result.is_err() {
        task.abort();
    }
    result.expect("async cleanup panicked or did not finish within ten seconds");
}

fn late_event() -> BleEvent {
    BleEvent::CommandCompleted {
        command_id: CommandId { value: 99 },
        error: None,
    }
}

fn assert_commands_closed(app: &App<MockRuntime>, backend: &FakeBackendHandle) {
    let before = backend.commands();
    let initialize = commands::initialize(app.state(), WireId(100)).unwrap_err();
    let stop_scan = commands::stop_scan(
        app.state(),
        commands::StopScanRequest {
            command_id: WireId(101),
            scan_session_id: WireId(1),
        },
    )
    .unwrap_err();
    for error in [initialize, stop_scan] {
        assert_eq!(error.category, commands::CommandErrorCategory::Closed);
    }
    assert_eq!(
        backend.commands(),
        before,
        "closed commands reached the backend"
    );
}

fn assert_retired(app: &App<MockRuntime>, backend: &FakeBackendHandle, owner: &Weak<FakeBackend>) {
    // FakeBackend counts effective cleanup, not every call to dispose().
    assert_eq!(backend.dispose_count(), 1);
    assert!(backend.ingress_for_callback().is_none());
    assert!(
        owner.upgrade().is_none(),
        "the Hub still owns the backend object"
    );
    assert_commands_closed(app, backend);
}

#[test]
fn concurrent_and_repeated_async_close_retire_backend_once() {
    let (app, backend, owner) = mock_plugin_app([]);
    let callback = backend.ingress_for_callback().unwrap();
    let state = app.state::<PluginState>().inner().clone();
    await_cleanup(async move {
        // Poll independent close callers together; no particular blocking-pool
        // scheduling or overlap inside FakeBackend::dispose is assumed.
        let results = futures_util::future::join_all((0..8).map(|_| state.close())).await;
        for result in results {
            result.expect("concurrent close");
        }
        state.close().await.expect("repeat close after completion");
        state.close().await.expect("repeat close again");
    });

    assert_retired(&app, &backend, &owner);
    assert_eq!(callback.push(late_event()), EventIngressStatus::Closed);
    assert!(app.handle().remove_plugin("blehub"));
    assert!(!app.handle().remove_plugin("blehub"));
    assert_retired(&app, &backend, &owner);
}

#[test]
fn removing_plugin_without_frontend_close_releases_backend_with_state_still_alive() {
    let (app, backend, owner) = mock_plugin_app([]);
    let state = app.state::<PluginState>().inner().clone();
    let callback = backend.ingress_for_callback().unwrap();

    // No close()/stop()/retire() before removal: cleanup must come from Tauri's
    // registered plugin on_drop, not from the final Arc<BleHub> being dropped.
    assert!(app.handle().remove_plugin("blehub"));
    assert!(Arc::ptr_eq(&state.hub, &app.state::<PluginState>().hub));
    assert_retired(&app, &backend, &owner);
    assert_eq!(callback.push(late_event()), EventIngressStatus::Closed);

    await_cleanup(async move {
        state.close().await.expect("close after plugin removal");
    });
    assert!(!app.handle().remove_plugin("blehub"));
    assert_retired(&app, &backend, &owner);
}

#[test]
fn duplicate_install_is_rejected_without_replacing_or_retiring_original_state() {
    let command = RecordedCommand::Initialize(InitializeCommand {
        command_id: CommandId { value: 1 },
    });
    let (app, original, owner) = mock_plugin_app([EventScenario::accept(command.clone())]);
    let state = app.state::<PluginState>().inner().clone();
    let (replacement, rejected) = FakeBackend::scripted([]);
    let replacement = Arc::new(replacement);
    let replacement_owner = Arc::downgrade(&replacement);

    let error = app.handle().plugin(with_backend(replacement)).unwrap_err();
    assert!(matches!(
        error,
        tauri::Error::PluginInitialization(name, detail)
            if name == "blehub" && detail == "BleHub is already installed in this app"
    ));
    let current = app.state::<PluginState>();
    assert!(Arc::ptr_eq(&state.hub, &current.hub));
    assert!(Arc::ptr_eq(&state.events, &current.events));
    assert_eq!(original.dispose_count(), 0);
    assert!(original.ingress_for_callback().is_some());
    assert!(owner.upgrade().is_some());
    assert!(replacement_owner.upgrade().is_none());
    assert!(rejected.ingress_for_callback().is_none());
    assert_eq!(
        rejected.dispose_count(),
        0,
        "rejected provider was never assembled"
    );
    assert!(rejected.commands().is_empty());

    commands::initialize(app.state(), WireId(1)).expect("original backend remains usable");
    assert_eq!(original.commands(), vec![command]);
    // A successful removal also proves the failed registration kept the old
    // plugin in Tauri's store, not just its app-managed state.
    assert!(app.handle().remove_plugin("blehub"));
    assert_retired(&app, &original, &owner);
    assert_eq!(rejected.dispose_count(), 0);
}

#[test]
fn removal_does_not_allow_reinstall_over_retained_closed_state() {
    let (app, original, owner) = mock_plugin_app([]);
    let state = app.state::<PluginState>().inner().clone();
    assert!(app.handle().remove_plugin("blehub"));
    assert_retired(&app, &original, &owner);

    // Tauri removal does not unmanage PluginState. The current plugin policy
    // rejects reinstallation even though the original plugin was unregistered.
    let (replacement, rejected) = FakeBackend::scripted([]);
    let error = app
        .handle()
        .plugin(with_backend(Arc::new(replacement)))
        .unwrap_err();
    assert!(matches!(
        error,
        tauri::Error::PluginInitialization(name, detail)
            if name == "blehub" && detail == "BleHub is already installed in this app"
    ));
    let current = app.state::<PluginState>();
    assert!(Arc::ptr_eq(&state.hub, &current.hub));
    assert!(Arc::ptr_eq(&state.events, &current.events));
    assert!(!app.handle().remove_plugin("blehub"));
    assert_retired(&app, &original, &owner);
    assert!(rejected.ingress_for_callback().is_none());
    assert_eq!(rejected.dispose_count(), 0);
    assert!(rejected.commands().is_empty());
}
