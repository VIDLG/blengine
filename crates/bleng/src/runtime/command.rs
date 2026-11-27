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
///
/// The route deliberately stops at the owning root. Which connection operation it
/// is — read, MTU, bond, a subscription transition — is already stated by the
/// waiter expectation the route was built from, and by the reservation's own
/// progress slot; repeating it here would be a third list to keep in sync.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum CommandRoute {
    Initialization,
    AdapterControl,
    DevicesQuery,
    CompanionAssociation,
    ScanStart,
    ScanStop,
    Connection {
        connection_id: ConnectionId,
    },
    #[cfg(test)]
    Test,
}

impl From<WaiterExpectation> for CommandRoute {
    fn from(expectation: WaiterExpectation) -> Self {
        match expectation {
            WaiterExpectation::Initialized => Self::Initialization,
            WaiterExpectation::AdapterControl => Self::AdapterControl,
            WaiterExpectation::DevicesListed => Self::DevicesQuery,
            WaiterExpectation::CompanionAssociation => Self::CompanionAssociation,
            WaiterExpectation::ScanStarted(_) => Self::ScanStart,
            WaiterExpectation::ScanStopped(_) => Self::ScanStop,
            WaiterExpectation::Connected(connection_id)
            | WaiterExpectation::Disconnected(connection_id)
            | WaiterExpectation::RssiRead(connection_id)
            | WaiterExpectation::MtuRead(connection_id)
            | WaiterExpectation::ConnectionPriority(connection_id)
            | WaiterExpectation::BondState(connection_id)
            | WaiterExpectation::BondRemoval(connection_id)
            | WaiterExpectation::GattCacheRefreshed(connection_id)
            | WaiterExpectation::GattDiscovered(connection_id, ..)
            | WaiterExpectation::CharacteristicRead(connection_id, ..)
            | WaiterExpectation::CharacteristicWritten(connection_id, ..)
            | WaiterExpectation::DescriptorRead(connection_id, ..)
            | WaiterExpectation::DescriptorWritten(connection_id, ..)
            | WaiterExpectation::NotificationsEnabled(connection_id, ..)
            | WaiterExpectation::NotificationsDisabled(connection_id, ..) => {
                Self::Connection { connection_id }
            }
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
