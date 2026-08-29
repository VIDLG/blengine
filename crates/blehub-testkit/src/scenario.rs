use blehub::{BleEvent, CommandDispatchError, CommandKind};

#[derive(Clone, Debug)]
pub enum ScenarioDispatch {
    Accept,
    Reject(CommandDispatchError),
    EmitBeforeReturn(Vec<BleEvent>),
    EmitBeforeReject {
        events: Vec<BleEvent>,
        error: CommandDispatchError,
    },
}

#[derive(Clone, Debug)]
pub struct EventScenario {
    pub operation: CommandKind,
    pub dispatch: ScenarioDispatch,
}

impl EventScenario {
    pub fn accept(operation: CommandKind) -> Self {
        Self {
            operation,
            dispatch: ScenarioDispatch::Accept,
        }
    }

    pub fn reject(operation: CommandKind, error: CommandDispatchError) -> Self {
        Self {
            operation,
            dispatch: ScenarioDispatch::Reject(error),
        }
    }

    pub fn emit_before_return(operation: CommandKind, events: Vec<BleEvent>) -> Self {
        Self {
            operation,
            dispatch: ScenarioDispatch::EmitBeforeReturn(events),
        }
    }

    pub fn emit_before_reject(
        operation: CommandKind,
        events: Vec<BleEvent>,
        error: CommandDispatchError,
    ) -> Self {
        Self {
            operation,
            dispatch: ScenarioDispatch::EmitBeforeReject { events, error },
        }
    }
}
