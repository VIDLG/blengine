use blehub::{
    BleEvent, CommandDispatchError, CommandId, ConnectionId, GattCharacteristicId, GattSessionId,
    NotificationMode, NotificationModeRequest, NotificationState, SubscriptionId,
};

use crate::waiter::{WaiterError, WaiterOutcome, WaiterSuccess};

use super::{
    BufferedEventDisposition, CommandProgress, DispatchAction, DispatchResolution,
    ReconciliationFault, is_connection_lost,
};

/// A subscription becomes active only after dispatch acknowledgement, a successful
/// terminal, and matching domain evidence. Disabling preserves its active route.
pub(super) struct SubscriptionReservation {
    pub(super) session_id: GattSessionId,
    pub(super) characteristic_id: GattCharacteristicId,
    phase: SubscriptionPhase,
}

enum SubscriptionPhase {
    Enabling {
        mode: NotificationModeRequest,
        transition: NotificationTransition,
    },
    Active,
    Disabling(NotificationTransition),
}

pub(super) enum SubscriptionDisposition {
    Keep,
    Remove,
}

/// A completed command can retire while its subscription route remains active.
/// The owner must apply Remove to the route collection before using it again.
pub(super) struct SubscriptionTransitionRetirement {
    pub(super) command_id: CommandId,
    pub(super) disposition: SubscriptionDisposition,
}

impl SubscriptionReservation {
    /// Creates the provisional route and its enable transition together.
    pub(super) fn enabling(
        command_id: CommandId,
        session_id: GattSessionId,
        characteristic_id: GattCharacteristicId,
        mode: NotificationModeRequest,
    ) -> Self {
        Self {
            session_id,
            characteristic_id,
            phase: SubscriptionPhase::enabling(command_id, mode),
        }
    }

    delegate::delegate! {
        to self.phase {
            /// Installs disable after admission and submission claim, without native I/O.
            /// Overwriting an unfinished transition is an internal invariant violation.
            pub(super) fn begin_disable(&mut self, command_id: CommandId);
            /// The local active route is retained during disable; this does not prove
            /// continued native notification delivery, including after a failed disable.
            pub(super) fn is_active(&self) -> bool;
            pub(super) fn is_transitioning(&self) -> bool;
            pub(super) fn progress(&self) -> Option<&CommandProgress>;
            pub(super) fn progress_mut(&mut self) -> Option<&mut CommandProgress>;
            /// Call after waiter settlement, even when the caller already retired.
            /// None means no transition or incomplete evidence, including a pending cancel result.
            /// Some identifies the completed command and independently decides route retention.
            /// Successful transitions require domain evidence; failed terminals do not.
            /// The owner must immediately apply Remove to its route collection.
            pub(super) fn retire_transition(&mut self) -> Option<SubscriptionTransitionRetirement>;
            /// Restores local Active for a failed disable, or requests removal of a failed enable.
            /// This is local route retention, not evidence of native rollback or continued values.
            /// Dispatch rejection drains buffered events before calling this; accepted
            /// terminal failure reaches it after waiter settlement through retire_transition().
            /// The collection owner must remove the route when Remove is returned.
            pub(super) fn reject_transition(&mut self) -> SubscriptionDisposition;
        }
    }

    /// Local subscription checks precede the parent owner's connection checks.
    pub(super) fn validate_disable(
        &self,
        session_id: GattSessionId,
    ) -> Result<GattCharacteristicId, WaiterError> {
        if self.session_id != session_id || !self.is_active() {
            return Err(WaiterError::SubscriptionNotActive);
        }
        if self.is_transitioning() {
            return Err(WaiterError::OperationBusy);
        }
        Ok(self.characteristic_id)
    }

    /// Transfers buffered evidence before changing the phase or releasing its progress.
    /// The collection must immediately apply the returned route disposition.
    pub(super) fn resolve_dispatch(
        &mut self,
        command_id: CommandId,
        result: &Result<(), CommandDispatchError>,
        connection_id: ConnectionId,
    ) -> Option<(DispatchResolution, SubscriptionDisposition)> {
        let progress = self
            .progress_mut()
            .filter(|progress| progress.owns_command(command_id))?;
        if result.is_ok() {
            return Some((
                progress.resolve_accepted_dispatch(),
                SubscriptionDisposition::Keep,
            ));
        }

        let resolution = progress.resolve_rejected_dispatch(DispatchAction::NoAction, |event| {
            // Unlike ordinary I/O, subscription rejection also permits commanded loss.
            if is_connection_lost(event, connection_id) {
                BufferedEventDisposition::Replay
            } else {
                BufferedEventDisposition::Conflict
            }
        });
        Some((resolution, self.reject_transition()))
    }

    // Dispatch gating and identity-validated evidence.

    /// Only an unresolved transition can buffer; an active route has no target.
    pub(super) fn should_buffer_event(
        &self,
        event: &BleEvent,
        expected: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> bool {
        self.phase.is_awaiting_dispatch_result()
            && self.matches_related_event(event, expected, subscription_id)
    }

    pub(super) fn record_event(
        &mut self,
        event: &BleEvent,
        connection_id: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> Result<(), ReconciliationFault> {
        if !self.matches_related_event(event, connection_id, subscription_id) {
            return Ok(());
        }
        let BleEvent::NotificationStateChanged { state, .. } = event else {
            return Ok(());
        };
        self.phase.record_state(state)
    }

    // Caller results retain the subscription identity.

    /// Queries a deliverable transition result without changing the route.
    /// None means no matching transition or incomplete evidence, not failure.
    pub(super) fn waiter_result(
        &self,
        command_id: CommandId,
        connection_id: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> Option<WaiterOutcome> {
        let result = self.phase.transition_result(command_id)?;
        Some(result.map(|state| match state {
            NotificationState::Enabled { actual_mode } => WaiterSuccess::NotificationsEnabled {
                connection_id,
                gatt_session_id: self.session_id,
                subscription_id,
                characteristic_id: self.characteristic_id,
                actual_mode,
            },
            NotificationState::Disabled => WaiterSuccess::NotificationsDisabled(
                connection_id,
                self.session_id,
                subscription_id,
                self.characteristic_id,
            ),
        }))
    }

    // Private event correlation.

    /// Correlates transition state/data, terminal, or connection loss, not enable success.
    /// record_event() separately checks the domain variant and required mode.
    fn matches_related_event(
        &self,
        event: &BleEvent,
        expected: ConnectionId,
        subscription_id: SubscriptionId,
    ) -> bool {
        if !self.phase.is_transitioning() {
            return false;
        }
        match event {
            BleEvent::NotificationStateChanged {
                command_id,
                connection_id,
                gatt_session_id,
                subscription_id: event_subscription,
                characteristic_id,
                ..
            } => {
                self.phase.owns_command(*command_id)
                    && expected == *connection_id
                    && self.session_id == *gatt_session_id
                    && subscription_id == *event_subscription
                    && self.characteristic_id == *characteristic_id
            }
            // Data has no command ID, but must not overtake earlier state or
            // terminal callbacks in this transition's existing dispatch buffer.
            BleEvent::NotificationReceived {
                connection_id,
                gatt_session_id,
                subscription_id: event_subscription,
                characteristic_id,
                ..
            } => {
                expected == *connection_id
                    && self.session_id == *gatt_session_id
                    && subscription_id == *event_subscription
                    && self.characteristic_id == *characteristic_id
            }
            BleEvent::CommandCompleted { command_id, .. } => self.phase.owns_command(*command_id),
            _ => is_connection_lost(event, expected),
        }
    }
}

impl SubscriptionPhase {
    fn enabling(command_id: CommandId, mode: NotificationModeRequest) -> Self {
        Self::Enabling {
            mode,
            transition: NotificationTransition::new(command_id),
        }
    }

    fn begin_disable(&mut self, command_id: CommandId) {
        assert!(
            matches!(self, Self::Active),
            "validated subscription must be active without a transition"
        );
        *self = Self::Disabling(NotificationTransition::new(command_id));
    }

    // Phase-local queries and command access.

    fn is_active(&self) -> bool {
        matches!(self, Self::Active | Self::Disabling(_))
    }

    delegate::delegate! {
        to self.transition() {
            #[call(is_some)]
            fn is_transitioning(&self) -> bool;
        }
    }

    fn owns_command(&self, command_id: CommandId) -> bool {
        self.progress()
            .is_some_and(|progress| progress.owns_command(command_id))
    }

    fn is_awaiting_dispatch_result(&self) -> bool {
        self.progress()
            .is_some_and(CommandProgress::is_awaiting_dispatch_result)
    }

    fn progress(&self) -> Option<&CommandProgress> {
        self.transition().map(|transition| &transition.progress)
    }

    fn progress_mut(&mut self) -> Option<&mut CommandProgress> {
        match self {
            Self::Enabling { transition, .. } | Self::Disabling(transition) => {
                Some(&mut transition.progress)
            }
            Self::Active => None,
        }
    }

    // Evidence policy, result selection and lifecycle transitions.

    /// Records an observed state only after the reservation validated event identity.
    fn record_state(&mut self, state: &NotificationState) -> Result<(), ReconciliationFault> {
        match (self, state) {
            (Self::Enabling { mode, transition }, NotificationState::Enabled { actual_mode }) => {
                transition.record_enabled(mode, *actual_mode)
            }
            (Self::Disabling(transition), NotificationState::Disabled) => {
                transition.record_disabled();
                Ok(())
            }
            _ => Ok(()),
        }
    }

    delegate::delegate! {
        to self.transition()? {
            #[call(result_for_cmd)]
            fn transition_result(
                &self,
                command_id: CommandId,
            ) -> Option<Result<NotificationState, WaiterError>>;
        }
    }

    fn retire_transition(&mut self) -> Option<SubscriptionTransitionRetirement> {
        let transition = self.transition()?;
        if !transition.is_complete() {
            return None;
        }
        let command_id = transition.command_id();
        let disposition = if transition.has_failed_terminal() {
            self.reject_transition()
        } else {
            match self {
                Self::Enabling { .. } => {
                    *self = Self::Active;
                    SubscriptionDisposition::Keep
                }
                Self::Disabling(_) => SubscriptionDisposition::Remove,
                Self::Active => return None,
            }
        };
        Some(SubscriptionTransitionRetirement {
            command_id,
            disposition,
        })
    }

    fn reject_transition(&mut self) -> SubscriptionDisposition {
        match self {
            Self::Enabling { .. } => SubscriptionDisposition::Remove,
            Self::Disabling(_) => {
                *self = Self::Active;
                SubscriptionDisposition::Keep
            }
            Self::Active => SubscriptionDisposition::Keep,
        }
    }

    // Stable Active has no retained transition.

    fn transition(&self) -> Option<&NotificationTransition> {
        match self {
            Self::Enabling { transition, .. } | Self::Disabling(transition) => Some(transition),
            Self::Active => None,
        }
    }
}

struct NotificationTransition {
    progress: CommandProgress,
    domain_evidence: Option<NotificationState>,
}

impl NotificationTransition {
    fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
            domain_evidence: None,
        }
    }

    delegate::delegate! {
        to self.progress {
            fn command_id(&self) -> CommandId;
            fn has_failed_terminal(&self) -> bool;
        }
    }

    fn record_enabled(
        &mut self,
        requested_mode: &NotificationModeRequest,
        actual_mode: Option<NotificationMode>,
    ) -> Result<(), ReconciliationFault> {
        // Validate even a duplicate before preserving the first evidence.
        match (requested_mode, actual_mode) {
            (NotificationModeRequest::PlatformDefault, _)
            | (
                NotificationModeRequest::RequireNotification,
                Some(NotificationMode::Notification),
            )
            | (NotificationModeRequest::RequireIndication, Some(NotificationMode::Indication)) => {}
            _ => return Err(ReconciliationFault::UnexpectedNotificationMode),
        }
        self.domain_evidence
            .get_or_insert(NotificationState::Enabled { actual_mode });
        Ok(())
    }

    fn record_disabled(&mut self) {
        self.domain_evidence
            .get_or_insert(NotificationState::Disabled);
    }

    /// Joins command, domain and cancellation evidence; the phase still decides
    /// whether the subscription route can be retained or must be removed.
    fn is_complete(&self) -> bool {
        self.progress.is_complete(self.domain_evidence.is_some())
    }

    /// Selects a result for this command without retiring it. Terminal failure
    /// takes precedence over domain evidence; cancellation can still block retirement.
    fn result_for_cmd(
        &self,
        command_id: CommandId,
    ) -> Option<Result<NotificationState, WaiterError>> {
        if !self.progress.owns_command(command_id) {
            return None;
        }
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(self.domain_evidence?))
    }
}

#[cfg(test)]
#[path = "tests/subscription.rs"]
mod tests;
