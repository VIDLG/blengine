//! Actor-side event coordination; reservations own correlation and evidence.
//!
//! Ingress probes owners in the established pre-ack order. A buffered event is
//! not published until dispatch resolves. Delivery then publishes, records,
//! settles waiters, and only afterwards retires completed reservations.
//! Reservations return local outcomes, never access lifecycle or waiter delivery.

use blehub::{BleEvent, CommandDispatchError, CommandId};
use kameo::prelude::{Context, Message};

use crate::{
    runtime::{
        IngressEvent, RuntimePhase,
        reservations::{DispatchEffect, DispatchResolution, ReconcileFault, connection_lost},
    },
    waiter::WaiterError,
};

use super::BlengActor;

/// Native dispatch acknowledgement delivered back through the actor mailbox.
pub(super) struct DispatchResolved {
    pub(super) id: CommandId,
    pub(super) result: Result<(), CommandDispatchError>,
}

impl BlengActor {
    fn reconciliation_fault(&self, fault: ReconcileFault) {
        tracing::error!(
            ?fault,
            "BLE reservation reconciliation failed; closing runtime"
        );
        self.lifecycle.fault();
    }

    fn deliver(&mut self, event: BleEvent) {
        if self.publish_event(&event).is_err() {
            return;
        }
        // Publication cannot be retracted if admission raced concurrent shutdown.
        if self.lifecycle.phase() != RuntimePhase::Open
            || self.connections.is_foreign_disconnect(&event)
        {
            return;
        }
        if let Err(fault) = self.record_event_evidence(&event) {
            self.reconciliation_fault(fault);
            return;
        }
        if self.lifecycle.phase() != RuntimePhase::Open {
            return;
        }
        self.settle_event_waiters(&event);
        self.retire_completed_state();
    }

    fn record_event_evidence(&mut self, event: &BleEvent) -> Result<(), ReconcileFault> {
        match event {
            BleEvent::AdapterSnapshotChanged { .. } => self.initialization.record_event(event),
            BleEvent::ScanStarted { .. } | BleEvent::ScanStopped { .. } => {
                self.scan.record_event(event)
            }
            BleEvent::CommandCompleted { command_id, error } => {
                self.record_terminal(*command_id, error.clone());
                Ok(())
            }
            _ => self.connections.record_event(event),
        }
    }

    /// Local caller settlement stays separate from reservation evidence mutation.
    fn settle_event_waiters(&mut self, event: &BleEvent) {
        if let BleEvent::ConnectionStateChanged { connection_id, .. } = event
            && connection_lost(event, *connection_id)
        {
            self.fail_connection_waiters(*connection_id);
            // Passive loss may complete an explicit disconnect's domain evidence,
            // but its own terminal is still required for successful settlement.
            if let Some(command_id) = self.connections.disconnect_command(*connection_id) {
                self.settle_waiter(command_id);
            }
        }
        if let Some(command_id) = event.command_id() {
            self.settle_waiter(command_id);
        }
        if matches!(event, BleEvent::ScanStopped { .. })
            && let Some(command_id) = self.scan.stop_command()
        {
            self.settle_waiter(command_id);
        }
    }

    /// Always run after waiter settlement, even for callbacks arriving after timeout.
    fn retire_completed_state(&mut self) {
        if self.initialization.has_failed() {
            // Accepted initialization failure invalidates even a caller-retired Hub.
            self.lifecycle.fault();
            return;
        }
        self.initialization.complete_if_ready();
        self.connections.retire_completed();
        self.scan.retire_completed();
    }

    fn buffer_before_dispatch(
        &mut self,
        event: BleEvent,
    ) -> Result<Option<BleEvent>, ReconcileFault> {
        // These are ownership probes, not broadcasts. Each connection owner also
        // preserves global operation-category priority within its collection.
        let Some(event) = self.initialization.buffer_before_dispatch(event)? else {
            return Ok(None);
        };
        let Some(event) = self.connections.buffer_before_dispatch(event)? else {
            return Ok(None);
        };
        self.scan.buffer_before_dispatch(event)
    }

    fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
    ) -> DispatchResolution {
        // Unique command IDs permit stopping at the first owning reservation.
        if let Some(resolution) = self.initialization.resolve_dispatch(command_id, result) {
            return resolution;
        }
        if let Some(resolution) = self.scan.resolve_dispatch(command_id, result) {
            return resolution;
        }
        self.connections
            .resolve_dispatch(command_id, result)
            .unwrap_or_default()
    }
}

impl Message<DispatchResolved> for BlengActor {
    type Reply = ();

    async fn handle(&mut self, message: DispatchResolved, _: &mut Context<Self, Self::Reply>) {
        let resolution = self.resolve_dispatch(message.id, &message.result);
        match resolution.effect {
            DispatchEffect::None => {}
            DispatchEffect::Cleanup => self.lifecycle.start_cleanup(),
            DispatchEffect::Fault(fault) => self.reconciliation_fault(fault),
        }
        if let Err(error) = message.result {
            // Rejection wins before replayed passive loss can affect this waiter.
            self.waiters
                .finish(message.id, Err(WaiterError::DispatchRejected(error)));
        }
        for event in resolution.replay {
            if self.lifecycle.phase() != RuntimePhase::Open {
                break;
            }
            self.deliver(event);
        }
    }
}

impl Message<IngressEvent> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        IngressEvent(event): IngressEvent,
        _: &mut Context<Self, Self::Reply>,
    ) {
        if self.lifecycle.phase() != RuntimePhase::Open {
            return;
        }
        // Foreign-command disconnects remain visible publicly, but must neither
        // enter another command's buffer nor mutate its native lifecycle.
        if self.connections.is_foreign_disconnect(&event) {
            self.deliver(event);
            return;
        }
        match self.buffer_before_dispatch(event) {
            Ok(Some(event)) => self.deliver(event),
            Ok(None) => {}
            Err(fault) => self.reconciliation_fault(fault),
        }
    }
}
