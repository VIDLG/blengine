use std::{
    collections::VecDeque,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};

use blehub::{
    BackendCapabilities, BackendEventIngressToken, BleBackend, BleHubError, CommandDispatchError,
    CommandKind, ConnectCommand, EventIngressStatus, Platform, StartScanCommand,
    push_backend_event,
};

use crate::{EventScenario, ScenarioDispatch};

struct FakeBackendState {
    token: Mutex<Option<BackendEventIngressToken>>,
    scenarios: Mutex<VecDeque<EventScenario>>,
    push_statuses: Mutex<Vec<EventIngressStatus>>,
    contract_violations: Mutex<Vec<BackendContractViolation>>,
    disposed: AtomicBool,
    dispose_count: AtomicUsize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BackendContractViolation {
    EventEmissionBeforeDispatchRejection {
        operation: CommandKind,
        emitted_event_count: usize,
    },
}

pub struct FakeBackend {
    state: Arc<FakeBackendState>,
}

#[derive(Clone)]
pub struct FakeBackendHandle {
    state: Arc<FakeBackendState>,
}

impl FakeBackend {
    pub fn scripted(
        scenarios: impl IntoIterator<Item = EventScenario>,
    ) -> (Self, FakeBackendHandle) {
        let state = Arc::new(FakeBackendState {
            token: Mutex::new(None),
            scenarios: Mutex::new(scenarios.into_iter().collect()),
            push_statuses: Mutex::new(Vec::new()),
            contract_violations: Mutex::new(Vec::new()),
            disposed: AtomicBool::new(false),
            dispose_count: AtomicUsize::new(0),
        });
        (
            Self {
                state: Arc::clone(&state),
            },
            FakeBackendHandle { state },
        )
    }

    fn dispatch(&self, operation: CommandKind) -> Result<(), CommandDispatchError> {
        if self.state.disposed.load(Ordering::Acquire) {
            return Err(CommandDispatchError::InvalidState {
                operation,
                message: "fake backend is disposed".to_owned(),
            });
        }
        let Some(scenario) = self.state.scenarios.lock().unwrap().pop_front() else {
            return Err(CommandDispatchError::InvalidState {
                operation,
                message: "no scripted scenario remains".to_owned(),
            });
        };
        if scenario.operation != operation {
            return Err(CommandDispatchError::InvalidState {
                operation,
                message: format!("expected {:?} scenario", scenario.operation),
            });
        }

        let pushes_before = self.state.push_statuses.lock().unwrap().len();
        let result = match scenario.dispatch {
            ScenarioDispatch::Accept => Ok(()),
            ScenarioDispatch::Reject(error) => Err(error),
            ScenarioDispatch::EmitBeforeReturn(events) => {
                self.push_events(operation, events)?;
                Ok(())
            }
            ScenarioDispatch::EmitBeforeReject { events, error } => {
                self.push_events(operation, events)?;
                Err(error)
            }
        };
        let pushes_after = self.state.push_statuses.lock().unwrap().len();
        if result.is_err() && pushes_after > pushes_before {
            self.state.contract_violations.lock().unwrap().push(
                BackendContractViolation::EventEmissionBeforeDispatchRejection {
                    operation,
                    emitted_event_count: pushes_after - pushes_before,
                },
            );
        }
        result
    }

    fn push_events(
        &self,
        operation: CommandKind,
        events: Vec<blehub::BleEvent>,
    ) -> Result<(), CommandDispatchError> {
        let token =
            self.state
                .token
                .lock()
                .unwrap()
                .ok_or_else(|| CommandDispatchError::InvalidState {
                    operation,
                    message: "event ingress is not bound".to_owned(),
                })?;
        let statuses = events
            .into_iter()
            .map(|event| push_backend_event(token, event))
            .collect::<Vec<_>>();
        self.state.push_statuses.lock().unwrap().extend(statuses);
        Ok(())
    }
}

impl BleBackend for FakeBackend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        Ok(BackendCapabilities::new(Platform::Test))
    }

    fn bind_event_ingress(&self, token: BackendEventIngressToken) -> Result<(), BleHubError> {
        let mut current = self.state.token.lock().unwrap();
        if current.is_some() {
            return Err(BleHubError::BackendAssembly {
                message: "event ingress is already bound".to_owned(),
            });
        }
        if !token.is_valid() {
            return Err(BleHubError::FfiContract {
                message: "event ingress token must be nonzero".to_owned(),
            });
        }
        *current = Some(token);
        Ok(())
    }

    fn start_scan(&self, _: StartScanCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(CommandKind::StartScan)
    }

    fn connect(&self, _: ConnectCommand) -> Result<(), CommandDispatchError> {
        self.dispatch(CommandKind::Connect)
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        if !self.state.disposed.swap(true, Ordering::AcqRel) {
            self.state.dispose_count.fetch_add(1, Ordering::AcqRel);
        }
        Ok(())
    }
}

impl FakeBackendHandle {
    pub fn emit(&self, event: blehub::BleEvent) -> EventIngressStatus {
        let Some(token) = *self.state.token.lock().unwrap() else {
            return EventIngressStatus::Closed;
        };
        let status = push_backend_event(token, event);
        self.state
            .push_statuses
            .lock()
            .unwrap()
            .push(status.clone());
        status
    }

    pub fn push_statuses(&self) -> Vec<EventIngressStatus> {
        self.state.push_statuses.lock().unwrap().clone()
    }

    pub fn dispose_count(&self) -> usize {
        self.state.dispose_count.load(Ordering::Acquire)
    }

    pub fn contract_violations(&self) -> Vec<BackendContractViolation> {
        self.state.contract_violations.lock().unwrap().clone()
    }
}

#[cfg(test)]
mod tests {
    use blehub::{
        BleEvent, BleEventPayload, BleHub, CommandId, CommandOutcome, ConnectionId,
        ConnectionPhase, PeripheralId, ScanSessionId,
    };

    use super::*;
    use crate::{RecordingSink, ReferenceProjection};

    fn scan_started(command_id: CommandId, scan_session_id: ScanSessionId) -> BleEvent {
        BleEvent {
            payload: BleEventPayload::ScanStarted {
                command_id,
                scan_session_id,
            },
        }
    }

    fn succeeded(command_id: CommandId) -> BleEvent {
        BleEvent {
            payload: BleEventPayload::CommandCompleted {
                command_id,
                outcome: CommandOutcome::Succeeded,
                error: None,
            },
        }
    }

    #[test]
    fn synchronous_events_before_return_are_delivered_and_projected() {
        let command_id = CommandId::new(7);
        let scan_session_id = ScanSessionId::new(11);
        let events = vec![
            scan_started(command_id, scan_session_id),
            succeeded(command_id),
        ];
        let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_return(
            CommandKind::StartScan,
            events,
        )]);
        let sink = RecordingSink::new();
        let hub = BleHub::new(Box::new(backend), Box::new(sink.clone())).unwrap();
        let mut projection = ReferenceProjection::default();
        assert!(projection.register_command(command_id));

        hub.start_scan(StartScanCommand {
            command_id,
            scan_session_id,
        })
        .unwrap();

        let delivered = sink.events();
        assert_eq!(delivered.len(), 2);
        for event in &delivered {
            projection.apply(event);
        }
        assert_eq!(
            projection.outcome(command_id),
            Some(CommandOutcome::Succeeded)
        );
        assert_eq!(
            handle.push_statuses(),
            vec![EventIngressStatus::Delivered, EventIngressStatus::Delivered]
        );
    }

    #[test]
    fn dispatch_rejection_emits_no_event() {
        let error = CommandDispatchError::BackendUnavailable {
            operation: CommandKind::Connect,
            message: "scripted rejection".to_owned(),
            platform: None,
        };
        let (backend, _) =
            FakeBackend::scripted([EventScenario::reject(CommandKind::Connect, error.clone())]);
        let sink = RecordingSink::new();
        let hub = BleHub::new(Box::new(backend), Box::new(sink.clone())).unwrap();

        assert_eq!(
            hub.connect(ConnectCommand {
                command_id: CommandId::new(1),
                connection_id: ConnectionId::new(2),
                peripheral_id: PeripheralId::new(3),
            }),
            Err(error)
        );
        assert!(sink.events().is_empty());
    }

    #[test]
    fn event_emission_before_dispatch_rejection_is_flagged_as_a_contract_violation() {
        let command_id = CommandId::new(7);
        let scan_session_id = ScanSessionId::new(11);
        let error = CommandDispatchError::BackendUnavailable {
            operation: CommandKind::StartScan,
            message: "rejected after emission".to_owned(),
            platform: None,
        };
        let (backend, handle) = FakeBackend::scripted([EventScenario::emit_before_reject(
            CommandKind::StartScan,
            vec![scan_started(command_id, scan_session_id)],
            error.clone(),
        )]);
        let sink = RecordingSink::new();
        let hub = BleHub::new(Box::new(backend), Box::new(sink.clone())).unwrap();

        assert_eq!(
            hub.start_scan(StartScanCommand {
                command_id,
                scan_session_id,
            }),
            Err(error)
        );
        assert_eq!(sink.events().len(), 1);
        assert_eq!(
            handle.contract_violations(),
            vec![
                BackendContractViolation::EventEmissionBeforeDispatchRejection {
                    operation: CommandKind::StartScan,
                    emitted_event_count: 1,
                }
            ]
        );
    }

    #[test]
    fn late_event_after_dispose_is_closed_and_cleanup_is_once() {
        let (backend, handle) = FakeBackend::scripted([]);
        let sink = RecordingSink::new();
        let hub = BleHub::new(Box::new(backend), Box::new(sink.clone())).unwrap();
        let event = BleEvent {
            payload: BleEventPayload::ConnectionStateChanged {
                command_id: None,
                connection_id: ConnectionId::new(9),
                phase: ConnectionPhase::Disconnected,
                error: None,
            },
        };

        hub.dispose().unwrap();
        hub.dispose().unwrap();

        assert_eq!(handle.emit(event), EventIngressStatus::Closed);
        assert!(sink.events().is_empty());
        assert_eq!(handle.dispose_count(), 1);
    }
}
