use std::{collections::VecDeque, sync::Mutex};

use blehub::{BleEvent, CommandDispatchError, CommandId, EventIngressStatus};

use crate::{
    BackendConformanceTracker, BackendContractViolation, CompletionExpectation, EventScenario,
    RecordedCommand, ScenarioDispatch,
};

pub(crate) struct ScenarioRunner {
    scenarios: Mutex<VecDeque<EventScenario>>,
    pending_events: Mutex<VecDeque<BleEvent>>,
    commands: Mutex<Vec<RecordedCommand>>,
    push_statuses: Mutex<Vec<EventIngressStatus>>,
    conformance: Mutex<BackendConformanceTracker>,
}

impl ScenarioRunner {
    pub(crate) fn new(scenarios: impl IntoIterator<Item = EventScenario>) -> Self {
        Self {
            scenarios: Mutex::new(scenarios.into_iter().collect()),
            pending_events: Mutex::new(VecDeque::new()),
            commands: Mutex::new(Vec::new()),
            push_statuses: Mutex::new(Vec::new()),
            conformance: Mutex::new(BackendConformanceTracker::default()),
        }
    }

    pub(crate) fn dispatch(
        &self,
        command: RecordedCommand,
        disposed: bool,
        mut emit: impl FnMut(BleEvent) -> Result<EventIngressStatus, CommandDispatchError>,
    ) -> Result<(), CommandDispatchError> {
        let command_id = command.command_id();
        self.commands.lock().unwrap().push(command.clone());
        self.conformance
            .lock()
            .unwrap()
            .begin_dispatch(command.clone());

        let result = (|| {
            if disposed {
                return Err(CommandDispatchError::InvalidState {
                    detail: "fake backend is disposed".to_owned(),
                });
            }
            let Some(scenario) = self.scenarios.lock().unwrap().pop_front() else {
                return Err(CommandDispatchError::InvalidState {
                    detail: "no scripted scenario remains".to_owned(),
                });
            };
            if scenario.expected_command != command {
                return Err(CommandDispatchError::InvalidState {
                    detail: format!(
                        "scripted command mismatch: expected {:?}, received {command:?}",
                        scenario.expected_command
                    ),
                });
            }

            let result = match scenario.dispatch {
                ScenarioDispatch::Accept => Ok(()),
                ScenarioDispatch::Reject(error) => Err(error),
                ScenarioDispatch::EmitBeforeReturn(events) => {
                    self.emit_all(events, &mut emit)?;
                    Ok(())
                }
                ScenarioDispatch::EmitBeforeReject { events, error } => {
                    self.emit_all(events, &mut emit)?;
                    Err(error)
                }
            };
            if result.is_ok() {
                self.pending_events
                    .lock()
                    .unwrap()
                    .extend(scenario.after_return);
            }
            result
        })();

        self.conformance
            .lock()
            .unwrap()
            .finish_dispatch(command_id, result.is_ok());
        result
    }

    pub(crate) fn advance(
        &self,
        emit: impl FnOnce(BleEvent) -> EventIngressStatus,
    ) -> Option<EventIngressStatus> {
        let event = self.pending_events.lock().unwrap().pop_front()?;
        let status = self.emit(event, emit);
        if status == EventIngressStatus::Closed {
            self.pending_events.lock().unwrap().clear();
        }
        Some(status)
    }

    pub(crate) fn emit(
        &self,
        event: BleEvent,
        emit: impl FnOnce(BleEvent) -> EventIngressStatus,
    ) -> EventIngressStatus {
        self.conformance
            .lock()
            .unwrap()
            .record_event_attempt(&event);
        let status = emit(event);
        self.push_statuses.lock().unwrap().push(status.clone());
        status
    }

    pub(crate) fn pending_event_count(&self) -> usize {
        self.pending_events.lock().unwrap().len()
    }

    pub(crate) fn push_statuses(&self) -> Vec<EventIngressStatus> {
        self.push_statuses.lock().unwrap().clone()
    }

    pub(crate) fn commands(&self) -> Vec<RecordedCommand> {
        self.commands.lock().unwrap().clone()
    }

    pub(crate) fn finish_completion(&self, command_id: CommandId, expected: CompletionExpectation) {
        self.conformance
            .lock()
            .unwrap()
            .finish_completion(command_id, expected);
    }

    pub(crate) fn terminal_attempted(&self, command_id: CommandId) -> bool {
        self.conformance
            .lock()
            .unwrap()
            .terminal_attempted(command_id)
    }

    pub(crate) fn contract_violations(&self) -> Vec<BackendContractViolation> {
        self.conformance.lock().unwrap().violations().to_vec()
    }

    fn emit_all(
        &self,
        events: Vec<BleEvent>,
        emit: &mut impl FnMut(BleEvent) -> Result<EventIngressStatus, CommandDispatchError>,
    ) -> Result<(), CommandDispatchError> {
        for event in events {
            self.conformance
                .lock()
                .unwrap()
                .record_event_attempt(&event);
            let status = emit(event)?;
            self.push_statuses.lock().unwrap().push(status);
        }
        Ok(())
    }
}
