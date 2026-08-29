#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Record)]
pub struct CommandId {
    pub value: u64,
}

impl CommandId {
    pub const fn new(value: u64) -> Self {
        Self { value }
    }

    pub const fn is_valid(self) -> bool {
        self.value != 0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Record)]
pub struct ScanSessionId {
    pub value: u64,
}

impl ScanSessionId {
    pub const fn new(value: u64) -> Self {
        Self { value }
    }

    pub const fn is_valid(self) -> bool {
        self.value != 0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Record)]
pub struct ConnectionId {
    pub value: u64,
}

impl ConnectionId {
    pub const fn new(value: u64) -> Self {
        Self { value }
    }

    pub const fn is_valid(self) -> bool {
        self.value != 0
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, uniffi::Record)]
pub struct PeripheralId {
    pub value: u64,
}

impl PeripheralId {
    pub const fn new(value: u64) -> Self {
        Self { value }
    }

    pub const fn is_valid(self) -> bool {
        self.value != 0
    }
}
