use std::collections::{HashMap, HashSet};

use blehub::{BleEvent, BleEventError, BleEventPayload, CommandId};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventDisposition {
    Accepted,
    Duplicate,
    Orphan,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProjectedCommandOutcome {
    Succeeded,
    Failed(BleEventError),
}

#[derive(Default)]
pub struct ReferenceProjection {
    pending: HashSet<CommandId>,
    outcomes: HashMap<CommandId, ProjectedCommandOutcome>,
}

impl ReferenceProjection {
    pub fn register_command(&mut self, command_id: CommandId) -> bool {
        command_id.is_valid()
            && !self.outcomes.contains_key(&command_id)
            && self.pending.insert(command_id)
    }

    pub fn apply(&mut self, event: &BleEvent) -> EventDisposition {
        let BleEventPayload::CommandCompleted { command_id, error } = &event.payload else {
            return EventDisposition::Accepted;
        };

        if self.outcomes.contains_key(command_id) {
            return EventDisposition::Duplicate;
        }
        if !self.pending.remove(command_id) {
            return EventDisposition::Orphan;
        }
        let outcome = match error {
            Some(error) => ProjectedCommandOutcome::Failed(error.clone()),
            None => ProjectedCommandOutcome::Succeeded,
        };
        self.outcomes.insert(*command_id, outcome);
        EventDisposition::Accepted
    }

    pub fn outcome(&self, command_id: CommandId) -> Option<ProjectedCommandOutcome> {
        self.outcomes.get(&command_id).cloned()
    }
}
