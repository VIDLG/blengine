use blehub::{BleEvent, CommandId, ConnectionId};

use crate::waiter::{WaiterOutcome, WaiterSuccess};

use super::super::{CommandProgress, is_connection_lost};

/// A GATT-cache refresh has no platform callback of its own.
///
/// The terminal `CommandCompleted` is the whole receipt: the platform accepted the refresh. The
/// engine separately drops its discovered topology for this connection, so old session capabilities
/// fail and the caller must rediscover.
pub(super) struct RefreshReservation {
    pub(super) progress: CommandProgress,
}

impl RefreshReservation {
    pub(super) fn new(command_id: CommandId) -> Self {
        Self {
            progress: CommandProgress::new(command_id),
        }
    }

    /// Buffers the terminal or a passive loss that arrives before dispatch acknowledgement.
    pub(super) fn should_buffer_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        self.progress.is_awaiting_dispatch_result() && self.matches_related_event(event, expected)
    }

    pub(super) fn waiter_result(&self, connection_id: ConnectionId) -> Option<WaiterOutcome> {
        if let Err(error) = self.progress.terminal_result()? {
            return Some(Err(error));
        }
        Some(Ok(WaiterSuccess::GattCacheRefreshed(connection_id)))
    }

    pub(super) fn is_retirable(&self) -> bool {
        self.progress.is_complete(true)
    }

    /// Correlates the terminal, or a loss on this connection; there is no domain value event.
    fn matches_related_event(&self, event: &BleEvent, expected: ConnectionId) -> bool {
        match event {
            BleEvent::CommandCompleted { command_id, .. } => {
                self.progress.owns_command(*command_id)
            }
            _ => is_connection_lost(event, expected),
        }
    }
}
