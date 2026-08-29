use boltffi::*;

pub struct BackendEventSinkHandle;

#[export]
impl BackendEventSinkHandle {
    pub fn push(&self, value: u32) -> u32 {
        value
    }
}

#[export]
pub trait BleBackend: Send + Sync {
    fn attach(&self, sink: BackendEventSinkHandle);
}
