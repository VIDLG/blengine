use std::collections::HashMap;

use blehub::{CommandId, ConnectionId};

use crate::waiter::WaiterExpectation;

use super::shared::CompletionObserver;

/// Runtime-only route metadata for one registered command.
///
/// The public `CommandId` remains an opaque correlation identity. This route is
/// coordinator-owned metadata used to select the reservation owner without probing
/// every root in sequence. Domain evidence and native retirement remain owned
/// by the selected reservation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum CommandRoute {
    Initialization,
    ScanStart,
    ScanStop,
    Connection {
        connection_id: ConnectionId,
        kind: ConnectionOperationKind,
    },
    #[cfg(test)]
    Test,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ConnectionOperationKind {
    Connect,
    Disconnect,
    DiscoverGatt,
    ReadRssi,
    Read,
    Write,
    EnableNotifications,
    DisableNotifications,
}

impl From<WaiterExpectation> for CommandRoute {
    fn from(expectation: WaiterExpectation) -> Self {
        match expectation {
            WaiterExpectation::Initialized => Self::Initialization,
            WaiterExpectation::ScanStarted(_) => Self::ScanStart,
            WaiterExpectation::ScanStopped(_) => Self::ScanStop,
            WaiterExpectation::Connected(connection_id) => Self::Connection {
                connection_id,
                kind: ConnectionOperationKind::Connect,
            },
            WaiterExpectation::Disconnected(connection_id) => Self::Connection {
                connection_id,
                kind: ConnectionOperationKind::Disconnect,
            },
            WaiterExpectation::RssiRead(connection_id) => Self::Connection {
                connection_id,
                kind: ConnectionOperationKind::ReadRssi,
            },
            WaiterExpectation::GattDiscovered(connection_id, _) => Self::Connection {
                connection_id,
                kind: ConnectionOperationKind::DiscoverGatt,
            },
            WaiterExpectation::CharacteristicRead(connection_id, _, _) => Self::Connection {
                connection_id,
                kind: ConnectionOperationKind::Read,
            },
            WaiterExpectation::CharacteristicWritten(connection_id, _, _) => Self::Connection {
                connection_id,
                kind: ConnectionOperationKind::Write,
            },
            WaiterExpectation::NotificationsEnabled(connection_id, _, _, _) => Self::Connection {
                connection_id,
                kind: ConnectionOperationKind::EnableNotifications,
            },
            WaiterExpectation::NotificationsDisabled(connection_id, _, _, _) => Self::Connection {
                connection_id,
                kind: ConnectionOperationKind::DisableNotifications,
            },
            #[cfg(test)]
            WaiterExpectation::TerminalOnly => Self::Test,
        }
    }
}

struct CommandEntry {
    route: CommandRoute,
    completion: Option<CompletionObserver>,
}

/// The reservation coordinator's source of truth for command routing metadata.
///
/// It does not own domain evidence or native retirement. Reservation roots keep
/// those lifetimes; the coordinator removes an entry only after its owner no
/// longer retains the command.
#[derive(Default)]
pub(crate) struct CommandDirectory {
    entries: HashMap<CommandId, CommandEntry>,
}

impl CommandDirectory {
    pub(super) fn insert(&mut self, command_id: CommandId, route: CommandRoute) {
        assert!(
            self.entries
                .insert(
                    command_id,
                    CommandEntry {
                        route,
                        completion: None,
                    },
                )
                .is_none(),
            "command ID route was registered twice"
        );
    }

    pub(super) fn route(&self, command_id: CommandId) -> Option<CommandRoute> {
        self.entries.get(&command_id).map(|entry| entry.route)
    }

    pub(super) fn set_completion(&mut self, command_id: CommandId, completion: CompletionObserver) {
        self.entries
            .get_mut(&command_id)
            .expect("completion route must be registered")
            .completion = Some(completion);
    }

    pub(super) fn completion(&self, command_id: CommandId) -> Option<CompletionObserver> {
        self.entries
            .get(&command_id)
            .and_then(|entry| entry.completion.clone())
    }

    pub(super) fn remove_many(&mut self, command_ids: impl IntoIterator<Item = CommandId>) {
        for command_id in command_ids {
            self.entries.remove(&command_id);
        }
    }
}
