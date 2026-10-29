use std::sync::{Arc, Mutex};

use blehub::{BleEvent, BleEventSink, EventSinkError};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SinkBehavior {
    Accept,
    Reject { message: String },
    Panic,
}

#[derive(Clone)]
pub struct RecordingSink {
    events: Arc<Mutex<Vec<BleEvent>>>,
    behavior: Arc<Mutex<SinkBehavior>>,
}

impl RecordingSink {
    pub fn new() -> Self {
        Self {
            events: Arc::new(Mutex::new(Vec::new())),
            behavior: Arc::new(Mutex::new(SinkBehavior::Accept)),
        }
    }

    pub fn set_behavior(&self, behavior: SinkBehavior) {
        *self.behavior.lock().unwrap() = behavior;
    }

    pub fn events(&self) -> Vec<BleEvent> {
        self.events.lock().unwrap().clone()
    }
}

impl Default for RecordingSink {
    fn default() -> Self {
        Self::new()
    }
}

impl BleEventSink for RecordingSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        let behavior = self.behavior.lock().unwrap().clone();
        match behavior {
            SinkBehavior::Accept => {
                self.events.lock().unwrap().push(event);
                Ok(())
            }
            SinkBehavior::Reject { message } => Err(EventSinkError::Rejected { detail: message }),
            SinkBehavior::Panic => panic!("recording sink panic requested by scenario"),
        }
    }
}
