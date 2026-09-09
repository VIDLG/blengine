use blehub::{
    BleEvent, CommandDispatchError, CommandId, CommandKind, ConnectCommand, DisconnectCommand,
    DiscoverGattCommand, InitializeCommand, ReadCharacteristicCommand, ReadRssiCommand,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecordedCommand {
    Initialize(InitializeCommand),
    StartScan(StartScanCommand),
    StopScan(StopScanCommand),
    Connect(ConnectCommand),
    Disconnect(DisconnectCommand),
    DiscoverGatt(DiscoverGattCommand),
    ReadCharacteristic(ReadCharacteristicCommand),
    WriteCharacteristic(WriteCharacteristicCommand),
    SetNotifications(SetNotificationsCommand),
    ReadRssi(ReadRssiCommand),
}

impl RecordedCommand {
    pub fn operation(&self) -> CommandKind {
        match self {
            Self::Initialize(_) => CommandKind::Initialize,
            Self::StartScan(_) => CommandKind::StartScan,
            Self::StopScan(_) => CommandKind::StopScan,
            Self::Connect(_) => CommandKind::Connect,
            Self::Disconnect(_) => CommandKind::Disconnect,
            Self::DiscoverGatt(_) => CommandKind::DiscoverGatt,
            Self::ReadCharacteristic(_) => CommandKind::ReadCharacteristic,
            Self::WriteCharacteristic(_) => CommandKind::WriteCharacteristic,
            Self::SetNotifications(_) => CommandKind::SetNotifications,
            Self::ReadRssi(_) => CommandKind::ReadRssi,
        }
    }

    pub fn command_id(&self) -> CommandId {
        match self {
            Self::Initialize(command) => command.command_id,
            Self::StartScan(command) => command.command_id,
            Self::StopScan(command) => command.command_id,
            Self::Connect(command) => command.command_id,
            Self::Disconnect(command) => command.command_id,
            Self::DiscoverGatt(command) => command.command_id,
            Self::ReadCharacteristic(command) => command.command_id,
            Self::WriteCharacteristic(command) => command.command_id,
            Self::SetNotifications(command) => command.command_id,
            Self::ReadRssi(command) => command.command_id,
        }
    }
}

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
    pub(crate) expected_command: RecordedCommand,
    pub(crate) dispatch: ScenarioDispatch,
    pub(crate) after_return: Vec<BleEvent>,
}

impl EventScenario {
    pub fn accept(expected_command: RecordedCommand) -> Self {
        Self {
            expected_command,
            dispatch: ScenarioDispatch::Accept,
            after_return: Vec::new(),
        }
    }

    pub fn complete(expected_command: RecordedCommand, events: Vec<BleEvent>) -> Self {
        Self {
            expected_command,
            dispatch: ScenarioDispatch::Accept,
            after_return: events,
        }
    }

    pub fn reject(expected_command: RecordedCommand, error: CommandDispatchError) -> Self {
        Self {
            expected_command,
            dispatch: ScenarioDispatch::Reject(error),
            after_return: Vec::new(),
        }
    }

    pub fn emit_before_return(expected_command: RecordedCommand, events: Vec<BleEvent>) -> Self {
        Self {
            expected_command,
            dispatch: ScenarioDispatch::EmitBeforeReturn(events),
            after_return: Vec::new(),
        }
    }

    pub fn emit_before_reject(
        expected_command: RecordedCommand,
        events: Vec<BleEvent>,
        error: CommandDispatchError,
    ) -> Self {
        Self {
            expected_command,
            dispatch: ScenarioDispatch::EmitBeforeReject { events, error },
            after_return: Vec::new(),
        }
    }
}
