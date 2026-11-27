//! One read-only system-inventory query.
//!
//! Like adapter control, this is a top-level operation with no connection, scan session or GATT
//! resource. Its evidence is the correlated `DevicesListed` payload plus the platform terminal; the
//! list is data, never a connectable capability. The shared lifecycle lives in [`TopLevelCommand`];
//! this shell adds the one thing that differs — the answer it carries.

use blehub::{BleEvent, BleEventError, CommandDispatchError, CommandId, DeviceRecord};

use super::super::shared::CompletionObserver;
use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::top_level::{TopLevelCommand, top_level_owner};
use super::{CancellationDecision, DispatchResolution, ReconciliationFault};

#[derive(Default)]
pub(in crate::runtime) struct DeviceQueryState {
    policy: TopLevelCommand<Vec<DeviceRecord>>,
}

impl DeviceQueryState {
    pub(in crate::runtime) fn require_available(&self) -> Result<(), WaiterError> {
        self.policy.require_available()
    }

    pub(in crate::runtime) fn reserve(&mut self, command_id: CommandId) {
        self.policy.reserve(command_id, "device query");
    }

    pub(super) fn completion_observer(&self, command_id: CommandId) -> Option<CompletionObserver> {
        self.policy.completion_observer(command_id)
    }

    /// This query's answer is a payload as well as a terminal, so both are withheld until dispatch
    /// is reconciled.
    pub(in crate::runtime) fn gate_event(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconciliationFault> {
        self.policy.gate_event(event, device_query_owner)
    }

    pub(in crate::runtime) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> Option<DispatchResolution> {
        self.policy.resolve_dispatch(command_id, result)
    }

    /// Records the inventory payload for this query. The first list wins.
    pub(in crate::runtime) fn record_event(&mut self, event: &BleEvent) {
        let BleEvent::DevicesListed {
            command_id,
            devices,
        } = event
        else {
            return;
        };
        self.policy.record_evidence(*command_id, devices.clone());
    }

    pub(in crate::runtime) fn record_terminal(
        &mut self,
        command_id: CommandId,
        result: &Result<(), BleEventError>,
    ) -> bool {
        self.policy.record_terminal(command_id, result)
    }

    pub(in crate::runtime) fn request_cancellation(
        &self,
        command_id: CommandId,
    ) -> Option<CancellationDecision> {
        self.policy.request_cancellation(command_id)
    }

    /// A successful terminal is not an inventory: the list is the answer, and a query that never
    /// received one stays pending.
    pub(in crate::runtime) fn waiter_result(&self, command_id: CommandId) -> Option<WaiterOutcome> {
        match self.policy.progress(command_id)?.terminal_result()? {
            Err(error) => Some(Err(error)),
            Ok(()) => Some(Ok(WaiterSuccess::DevicesListed(
                self.policy.evidence(command_id)?.clone(),
            ))),
        }
    }

    pub(in crate::runtime) fn retire_completed(&mut self) -> Option<CommandId> {
        self.policy.retire_completed(true)
    }
}

/// This command is identified by its terminal and by the inventory payload it produces.
fn device_query_owner(event: &BleEvent) -> Option<CommandId> {
    match event {
        BleEvent::DevicesListed { command_id, .. } => Some(*command_id),
        other => top_level_owner(other),
    }
}
