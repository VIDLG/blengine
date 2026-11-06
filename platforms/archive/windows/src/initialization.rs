use std::sync::{Arc, Condvar, Mutex};

use blehub::{
    AdapterSnapshot, AdapterState, AuthorizationState, BleErrorCategory, BleEvent, BleEventError,
    CommandDispatchError, CommandId, InitializeCommand,
};
use windows::{
    Devices::{Bluetooth::BluetoothAdapter, Radios::Radio},
    Foundation::TypedEventHandler,
    core::{IInspectable, Interface},
};
use windows_future::{AsyncOperationCompletedHandler, AsyncStatus, IAsyncOperation};

use crate::{
    backend::{
        BackendState, Lifecycle, WindowsBackend, delivery_for_state, ensure_ingress_bound,
        lock_delivery, lock_state, log_cleanup_failure, push_events, track_callback,
    },
    error::{dispatch_error, event_error, simple_event_error},
    mapping::map_adapter_snapshot,
    resource_accounting::CallbackResource,
};

pub(crate) struct InitializationCallbackBarrier {
    state: Mutex<InitializationCallbackBarrierState>,
    drained: Condvar,
}

struct InitializationCallbackBarrierState {
    closed: bool,
    active: usize,
}

struct InitializationCallbackGuard<'a> {
    barrier: &'a InitializationCallbackBarrier,
}

impl InitializationCallbackBarrier {
    pub(crate) fn new() -> Self {
        Self {
            state: Mutex::new(InitializationCallbackBarrierState {
                closed: false,
                active: 0,
            }),
            drained: Condvar::new(),
        }
    }

    fn enter(&self) -> Option<InitializationCallbackGuard<'_>> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.closed {
            return None;
        }
        state.active += 1;
        Some(InitializationCallbackGuard { barrier: self })
    }

    pub(crate) fn close_and_wait(&self) {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.closed = true;
        while state.active != 0 {
            state = self
                .drained
                .wait(state)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
    }
}

impl Drop for InitializationCallbackGuard<'_> {
    fn drop(&mut self) {
        let mut state = self
            .barrier
            .state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        state.active -= 1;
        if state.active == 0 {
            self.barrier.drained.notify_all();
        }
    }
}

pub(crate) enum RetiredInitializationOperation {
    Adapter(IAsyncOperation<BluetoothAdapter>),
    Radio(IAsyncOperation<Radio>),
}

impl RetiredInitializationOperation {
    pub(crate) fn cancel(&self) -> windows::core::Result<()> {
        match self {
            Self::Adapter(operation) => operation.Cancel(),
            Self::Radio(operation) => operation.Cancel(),
        }
    }

    pub(crate) fn cleanup_name(&self) -> &'static str {
        match self {
            Self::Adapter(_) => "cancel retired adapter lookup",
            Self::Radio(_) => "cancel retired radio lookup",
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
enum PendingOperationClaim<T> {
    Owned(T),
    Advanced,
    Closed,
}

fn claim_pending_operation<T>(
    lifecycle: Lifecycle,
    command_id: CommandId,
    operation: &mut Option<T>,
) -> PendingOperationClaim<T> {
    if lifecycle == Lifecycle::Closed {
        PendingOperationClaim::Closed
    } else if lifecycle != (Lifecycle::Initializing { command_id }) {
        PendingOperationClaim::Advanced
    } else {
        operation.take().map_or(
            PendingOperationClaim::Advanced,
            PendingOperationClaim::Owned,
        )
    }
}

pub(crate) fn begin(
    backend: &WindowsBackend,
    command: InitializeCommand,
) -> Result<(), CommandDispatchError> {
    let _setup_guard = backend
        .initialization_setup
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    {
        let mut state = lock_state(&backend.state);
        ensure_ingress_bound(&state)?;
        if state.lifecycle != Lifecycle::Inert {
            return Err(CommandDispatchError::InvalidState {
                detail: "Windows backend initialization has already started".to_owned(),
            });
        }
        state.lifecycle = Lifecycle::Initializing {
            command_id: command.command_id,
        };
    }

    let operation = match BluetoothAdapter::GetDefaultAsync() {
        Ok(operation) => operation,
        Err(error) => {
            let mut state = lock_state(&backend.state);
            if state.lifecycle
                == (Lifecycle::Initializing {
                    command_id: command.command_id,
                })
            {
                state.lifecycle = Lifecycle::Inert;
            }
            return Err(dispatch_error("get default Bluetooth adapter", error));
        }
    };
    {
        let mut state = lock_state(&backend.state);
        if state.lifecycle
            != (Lifecycle::Initializing {
                command_id: command.command_id,
            })
        {
            drop(state);
            log_cleanup_failure(
                operation.Cancel(),
                "cancel default Bluetooth adapter lookup after initialization closed",
            );
            return Err(CommandDispatchError::Closed);
        }
        state.adapter_operation = Some(operation.clone());
    }

    let weak_state = Arc::downgrade(&backend.state);
    let callback_barrier = Arc::clone(&backend.initialization_callbacks);
    let callback_resource = track_callback(&backend.state, CallbackResource::AdapterLookup);
    let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
        let _callback_resource = &callback_resource;
        let Some(_callback_guard) = callback_barrier.enter() else {
            return Ok(());
        };
        if let Some(state) = weak_state.upgrade() {
            handle_adapter_completed(
                &state,
                &callback_barrier,
                command.command_id,
                operation.as_ref(),
                status,
            );
        }
        Ok(())
    });
    if let Err(error) = operation.SetCompleted(&handler) {
        let delivery_gate = delivery_for_state(&backend.state);
        let delivery_guard = lock_delivery(&delivery_gate);
        let claimed_operation = {
            let mut state = lock_state(&backend.state);
            let lifecycle = state.lifecycle;
            match claim_pending_operation(
                lifecycle,
                command.command_id,
                &mut state.adapter_operation,
            ) {
                PendingOperationClaim::Owned(operation) => operation,
                PendingOperationClaim::Advanced => return Ok(()),
                PendingOperationClaim::Closed => return Err(CommandDispatchError::Closed),
            }
        };
        drop(delivery_guard);

        let cancellation = claimed_operation.Cancel();
        let _delivery_guard = lock_delivery(&delivery_gate);
        let mut state = lock_state(&backend.state);
        if state.lifecycle
            != (Lifecycle::Initializing {
                command_id: command.command_id,
            })
        {
            if let Err(cancel_error) = cancellation {
                tracing::warn!(
                    target: "blehub_windows::lifecycle",
                    operation = "cancel default Bluetooth adapter lookup after completion registration failure",
                    detail = %cancel_error,
                    "Windows initialization operation cancellation failed after lifecycle advanced"
                );
                if state.lifecycle != Lifecycle::Closed {
                    state
                        .retired_initialization_operations
                        .push(RetiredInitializationOperation::Adapter(claimed_operation));
                }
            }
            return if state.lifecycle == Lifecycle::Closed {
                Err(CommandDispatchError::Closed)
            } else {
                Ok(())
            };
        }
        match cancellation {
            Ok(()) => {
                state.lifecycle = Lifecycle::Inert;
                Err(dispatch_error("register default-adapter completion", error))
            }
            Err(cancel_error) => {
                tracing::warn!(
                    target: "blehub_windows::lifecycle",
                    operation = "cancel default Bluetooth adapter lookup after completion registration failure",
                    detail = %cancel_error,
                    "Windows initialization operation cancellation failed; retaining cleanup ownership"
                );
                state
                    .retired_initialization_operations
                    .push(RetiredInitializationOperation::Adapter(claimed_operation));
                state.adapter = None;
                state.radio = None;
                state.radio_state_token = None;
                state.lifecycle = Lifecycle::Faulted;
                let delivery = state.ingress().map(|ingress| {
                    (
                        ingress,
                        BleEvent::CommandCompleted {
                            command_id: command.command_id,
                            error: Some(event_error(
                                BleErrorCategory::Backend,
                                "register default-adapter completion",
                                error,
                            )),
                        },
                    )
                });
                drop(state);
                if let Some((ingress, event)) = delivery {
                    push_events(&ingress, vec![event], "fail Windows initialization setup");
                }
                Ok(())
            }
        }
    } else {
        Ok(())
    }
}

fn handle_adapter_completed(
    state: &Arc<Mutex<BackendState>>,
    callback_barrier: &Arc<InitializationCallbackBarrier>,
    command_id: CommandId,
    operation: Option<&IAsyncOperation<BluetoothAdapter>>,
    status: AsyncStatus,
) {
    let _operation_root = {
        let mut current = lock_state(state);
        let lifecycle = current.lifecycle;
        match claim_pending_operation(lifecycle, command_id, &mut current.adapter_operation) {
            PendingOperationClaim::Owned(operation) => operation,
            PendingOperationClaim::Advanced | PendingOperationClaim::Closed => return,
        }
    };
    if status != AsyncStatus::Completed {
        let error = if status == AsyncStatus::Canceled {
            simple_event_error(
                BleErrorCategory::Cancelled,
                "default Bluetooth adapter lookup was cancelled",
            )
        } else {
            simple_event_error(
                BleErrorCategory::Backend,
                format!("default Bluetooth adapter lookup ended with {status:?}"),
            )
        };
        fail_initialization(state, command_id, error);
        return;
    }
    let Some(operation) = operation else {
        fail_initialization(
            state,
            command_id,
            simple_event_error(
                BleErrorCategory::Internal,
                "default Bluetooth adapter completion omitted the operation",
            ),
        );
        return;
    };
    let adapter = match operation.GetResults() {
        Ok(adapter) => adapter,
        Err(error) => {
            fail_initialization(
                state,
                command_id,
                event_error(
                    BleErrorCategory::Backend,
                    "complete default Bluetooth adapter lookup",
                    error,
                ),
            );
            return;
        }
    };
    if Interface::as_raw(&adapter).is_null() {
        complete_unavailable_initialization(state, command_id);
        return;
    }
    let supports_le = match adapter.IsLowEnergySupported() {
        Ok(value) => value,
        Err(error) => {
            fail_initialization(
                state,
                command_id,
                event_error(
                    BleErrorCategory::Backend,
                    "query Bluetooth LE support",
                    error,
                ),
            );
            return;
        }
    };
    let supports_central = match adapter.IsCentralRoleSupported() {
        Ok(value) => value,
        Err(error) => {
            fail_initialization(
                state,
                command_id,
                event_error(
                    BleErrorCategory::Backend,
                    "query Bluetooth Central role support",
                    error,
                ),
            );
            return;
        }
    };
    if !supports_le || !supports_central {
        complete_unavailable_initialization(state, command_id);
        return;
    }

    let operation = match adapter.GetRadioAsync() {
        Ok(operation) => operation,
        Err(error) => {
            fail_initialization(
                state,
                command_id,
                event_error(BleErrorCategory::Backend, "get Bluetooth radio", error),
            );
            return;
        }
    };
    let reserved = {
        let mut current = lock_state(state);
        if current.lifecycle != (Lifecycle::Initializing { command_id }) {
            false
        } else {
            current.adapter = Some(adapter);
            current.radio_operation = Some(operation.clone());
            true
        }
    };
    if !reserved {
        cancel_or_retire_initialization_operation(
            state,
            RetiredInitializationOperation::Radio(operation),
            "cancel unreserved Bluetooth radio lookup",
        );
        return;
    }
    let weak_state = Arc::downgrade(state);
    let callback_barrier = Arc::clone(callback_barrier);
    let callback_resource = track_callback(state, CallbackResource::RadioLookup);
    let handler = AsyncOperationCompletedHandler::new(move |operation, status| {
        let _callback_resource = &callback_resource;
        let Some(_callback_guard) = callback_barrier.enter() else {
            return Ok(());
        };
        if let Some(state) = weak_state.upgrade() {
            handle_radio_completed(&state, command_id, operation.as_ref(), status);
        }
        Ok(())
    });
    if let Err(error) = operation.SetCompleted(&handler) {
        let claimed_operation = {
            let mut current = lock_state(state);
            let lifecycle = current.lifecycle;
            match claim_pending_operation(lifecycle, command_id, &mut current.radio_operation) {
                PendingOperationClaim::Owned(operation) => operation,
                PendingOperationClaim::Advanced | PendingOperationClaim::Closed => return,
            }
        };
        fail_initialization_with_operation(
            state,
            command_id,
            event_error(
                BleErrorCategory::Backend,
                "register Bluetooth radio completion",
                error,
            ),
            Some(RetiredInitializationOperation::Radio(claimed_operation)),
        );
    }
}

fn handle_radio_completed(
    state: &Arc<Mutex<BackendState>>,
    command_id: CommandId,
    operation: Option<&IAsyncOperation<Radio>>,
    status: AsyncStatus,
) {
    let _operation_root = {
        let mut current = lock_state(state);
        let lifecycle = current.lifecycle;
        match claim_pending_operation(lifecycle, command_id, &mut current.radio_operation) {
            PendingOperationClaim::Owned(operation) => operation,
            PendingOperationClaim::Advanced | PendingOperationClaim::Closed => return,
        }
    };
    if status != AsyncStatus::Completed {
        let error = if status == AsyncStatus::Canceled {
            simple_event_error(
                BleErrorCategory::Cancelled,
                "Bluetooth radio lookup was cancelled",
            )
        } else {
            simple_event_error(
                BleErrorCategory::Backend,
                format!("Bluetooth radio lookup ended with {status:?}"),
            )
        };
        fail_initialization(state, command_id, error);
        return;
    }
    let Some(operation) = operation else {
        fail_initialization(
            state,
            command_id,
            simple_event_error(
                BleErrorCategory::Internal,
                "Bluetooth radio completion omitted the operation",
            ),
        );
        return;
    };
    let radio = match operation.GetResults() {
        Ok(radio) => radio,
        Err(error) => {
            fail_initialization(
                state,
                command_id,
                event_error(
                    BleErrorCategory::Backend,
                    "complete Bluetooth radio lookup",
                    error,
                ),
            );
            return;
        }
    };
    let weak_state = Arc::downgrade(state);
    let callback_resource = track_callback(state, CallbackResource::RadioStateChanged);
    let state_handler = TypedEventHandler::<Radio, IInspectable>::new(move |sender, _args| {
        let _callback_resource = &callback_resource;
        if let (Some(state), Some(radio)) = (weak_state.upgrade(), sender.as_ref()) {
            handle_radio_state_changed(&state, radio);
        }
        Ok(())
    });
    let token = match radio.StateChanged(&state_handler) {
        Ok(token) => token,
        Err(error) => {
            fail_initialization(
                state,
                command_id,
                event_error(
                    BleErrorCategory::Backend,
                    "register Bluetooth radio-state callback",
                    error,
                ),
            );
            return;
        }
    };
    let delivery_gate = delivery_for_state(state);
    let delivery_guard = lock_delivery(&delivery_gate);
    let radio_state = match radio.State() {
        Ok(radio_state) => radio_state,
        Err(error) => {
            drop(delivery_guard);
            log_cleanup_failure(
                radio.RemoveStateChanged(token),
                "remove radio-state callback after initial state-read failure",
            );
            fail_initialization(
                state,
                command_id,
                event_error(
                    BleErrorCategory::Backend,
                    "read Bluetooth radio state",
                    error,
                ),
            );
            return;
        }
    };
    let name = radio.Name().ok().map(|name| name.to_string_lossy());
    let snapshot = map_adapter_snapshot(radio_state, name);
    let delivery = {
        let mut current = lock_state(state);
        if current.lifecycle != (Lifecycle::Initializing { command_id }) {
            drop(current);
            drop(delivery_guard);
            log_cleanup_failure(
                radio.RemoveStateChanged(token),
                "remove radio-state callback after initialization was closed",
            );
            return;
        }
        current.radio = Some(radio);
        current.radio_state_token = Some(token);
        current.adapter_snapshot = Some(snapshot.clone());
        current.lifecycle = Lifecycle::Ready;
        current.ingress().map(|ingress| {
            (
                ingress,
                vec![
                    BleEvent::AdapterSnapshotChanged {
                        command_id: Some(command_id),
                        snapshot,
                    },
                    BleEvent::CommandCompleted {
                        command_id,
                        error: None,
                    },
                ],
            )
        })
    };
    if let Some((ingress, events)) = delivery {
        push_events(&ingress, events, "complete Windows initialization");
    }
}

fn complete_unavailable_initialization(state: &Arc<Mutex<BackendState>>, command_id: CommandId) {
    let delivery_gate = delivery_for_state(state);
    let _delivery_guard = lock_delivery(&delivery_gate);
    complete_unavailable_initialization_locked(state, command_id);
}

fn complete_unavailable_initialization_locked(
    state: &Arc<Mutex<BackendState>>,
    command_id: CommandId,
) {
    let snapshot = AdapterSnapshot {
        state: AdapterState::Unavailable,
        scan_authorization: AuthorizationState::NotRequired,
        connection_authorization: AuthorizationState::NotRequired,
        name: None,
    };
    let delivery = {
        let mut current = lock_state(state);
        if current.lifecycle != (Lifecycle::Initializing { command_id }) {
            return;
        }
        current.adapter_operation = None;
        current.radio_operation = None;
        current.adapter = None;
        current.radio = None;
        current.adapter_snapshot = Some(snapshot.clone());
        current.lifecycle = Lifecycle::Ready;
        current.ingress().map(|ingress| {
            (
                ingress,
                vec![
                    BleEvent::AdapterSnapshotChanged {
                        command_id: Some(command_id),
                        snapshot,
                    },
                    BleEvent::CommandCompleted {
                        command_id,
                        error: None,
                    },
                ],
            )
        })
    };
    if let Some((ingress, events)) = delivery {
        push_events(
            &ingress,
            events,
            "complete unavailable Windows initialization",
        );
    }
}

fn fail_initialization(
    state: &Arc<Mutex<BackendState>>,
    command_id: CommandId,
    error: BleEventError,
) {
    fail_initialization_with_operation(state, command_id, error, None);
}

fn fail_initialization_with_operation(
    state: &Arc<Mutex<BackendState>>,
    command_id: CommandId,
    error: BleEventError,
    claimed_operation: Option<RetiredInitializationOperation>,
) {
    let delivery_gate = delivery_for_state(state);
    let delivery_guard = lock_delivery(&delivery_gate);
    let (delivery, adapter_operation, radio_operation) = {
        let mut current = lock_state(state);
        if current.lifecycle != (Lifecycle::Initializing { command_id }) {
            return;
        }
        let adapter_operation = current.adapter_operation.take();
        let radio_operation = current.radio_operation.take();
        current.adapter = None;
        current.radio = None;
        current.radio_state_token = None;
        current.lifecycle = Lifecycle::Faulted;
        let delivery = current.ingress().map(|ingress| {
            (
                ingress,
                BleEvent::CommandCompleted {
                    command_id,
                    error: Some(error),
                },
            )
        });
        (delivery, adapter_operation, radio_operation)
    };
    drop(delivery_guard);

    if let Some(operation) = claimed_operation {
        cancel_or_retire_initialization_operation(
            state,
            operation,
            "cancel claimed initialization operation after completion registration failure",
        );
    }
    if let Some(operation) = adapter_operation {
        cancel_or_retire_initialization_operation(
            state,
            RetiredInitializationOperation::Adapter(operation),
            "cancel adapter lookup after initialization failure",
        );
    }
    if let Some(operation) = radio_operation {
        cancel_or_retire_initialization_operation(
            state,
            RetiredInitializationOperation::Radio(operation),
            "cancel radio lookup after initialization failure",
        );
    }

    let _delivery_guard = lock_delivery(&delivery_gate);
    if let Some((ingress, event)) = delivery {
        push_events(&ingress, vec![event], "fail Windows initialization");
    }
}

fn handle_radio_state_changed(state: &Arc<Mutex<BackendState>>, radio: &Radio) {
    let delivery_gate = delivery_for_state(state);
    let _delivery_guard = lock_delivery(&delivery_gate);
    let radio_state = match radio.State() {
        Ok(value) => value,
        Err(error) => {
            let event = BleEvent::BackendError {
                error: event_error(
                    BleErrorCategory::Backend,
                    "read Bluetooth radio state in callback",
                    error,
                ),
            };
            let ingress = {
                let current = lock_state(state);
                (current.lifecycle == Lifecycle::Ready)
                    .then(|| current.ingress())
                    .flatten()
            };
            if let Some(ingress) = ingress {
                push_events(
                    &ingress,
                    vec![event],
                    "report Windows radio-state callback failure",
                );
            }
            return;
        }
    };
    let name = radio.Name().ok().map(|name| name.to_string_lossy());
    let snapshot = map_adapter_snapshot(radio_state, name);
    let ingress = {
        let mut current = lock_state(state);
        if current.lifecycle != Lifecycle::Ready {
            return;
        }
        current.adapter_snapshot = Some(snapshot.clone());
        current.ingress()
    };
    if let Some(ingress) = ingress {
        push_events(
            &ingress,
            vec![BleEvent::AdapterSnapshotChanged {
                command_id: None,
                snapshot,
            }],
            "deliver Windows radio-state change",
        );
    }
}

fn cancel_or_retire_initialization_operation(
    state: &Arc<Mutex<BackendState>>,
    operation: RetiredInitializationOperation,
    cleanup_name: &'static str,
) {
    if let Err(error) = operation.cancel() {
        tracing::warn!(
            target: "blehub_windows::lifecycle",
            operation = cleanup_name,
            detail = %error,
            "Windows initialization operation cancellation failed; retaining cleanup ownership"
        );
        let mut current = lock_state(state);
        if current.lifecycle != Lifecycle::Closed {
            current.retired_initialization_operations.push(operation);
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{sync::mpsc, thread, time::Duration};

    use super::*;

    #[test]
    fn initialization_operation_can_only_be_claimed_once() {
        let command_id = CommandId { value: 7 };
        let mut operation = Some("adapter operation");

        assert_eq!(
            claim_pending_operation(
                Lifecycle::Initializing { command_id },
                command_id,
                &mut operation,
            ),
            PendingOperationClaim::Owned("adapter operation")
        );
        assert_eq!(
            claim_pending_operation(
                Lifecycle::Initializing { command_id },
                command_id,
                &mut operation,
            ),
            PendingOperationClaim::Advanced
        );
    }

    #[test]
    fn initialization_operation_claim_rejects_stale_and_closed_lifecycles() {
        let command_id = CommandId { value: 7 };
        let mut stale_operation = Some("stale operation");
        let mut closed_operation = Some("closed operation");

        assert_eq!(
            claim_pending_operation(
                Lifecycle::Initializing {
                    command_id: CommandId { value: 8 },
                },
                command_id,
                &mut stale_operation,
            ),
            PendingOperationClaim::Advanced
        );
        assert_eq!(stale_operation, Some("stale operation"));
        assert_eq!(
            claim_pending_operation(Lifecycle::Closed, command_id, &mut closed_operation),
            PendingOperationClaim::Closed
        );
        assert_eq!(closed_operation, Some("closed operation"));
    }

    #[test]
    fn initialization_callback_barrier_waits_for_active_callbacks_and_stays_closed() {
        let barrier = Arc::new(InitializationCallbackBarrier::new());
        let callback_guard = barrier.enter().unwrap();
        let (started_sender, started_receiver) = mpsc::channel();
        let (drained_sender, drained_receiver) = mpsc::channel();
        let closer = {
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                started_sender.send(()).unwrap();
                barrier.close_and_wait();
                drained_sender.send(()).unwrap();
            })
        };

        started_receiver
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        assert!(
            drained_receiver
                .recv_timeout(Duration::from_millis(50))
                .is_err()
        );
        drop(callback_guard);
        drained_receiver
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        closer.join().unwrap();
        assert!(barrier.enter().is_none());
    }
}
