#![cfg(windows)]

use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};

use blehub::{
    BackendCapabilities, BackendEventIngressHandle, BleBackend, BleEvent, BleEventSink, BleHub,
    BleHubError, CommandDispatchError, CommandId, ConnectCommand, DisconnectCommand,
    DiscoverGattCommand, EventIngressStatus, EventSinkError, InitializeCommand, Platform,
    ReadCharacteristicCommand, ReadRssiCommand, ScanOptions, ScanSessionId,
    SetNotificationsCommand, StartScanCommand, StopScanCommand, WriteCharacteristicCommand,
};
use blehub_windows::WindowsBackend;

#[derive(Clone)]
struct RecordingSink {
    events: Arc<Mutex<Vec<BleEvent>>>,
}

impl BleEventSink for RecordingSink {
    fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
        self.events
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .push(event);
        Ok(())
    }
}

struct ConsumerBackend {
    ingress: Mutex<Option<Arc<BackendEventIngressHandle>>>,
    dispose_count: AtomicUsize,
}

impl ConsumerBackend {
    fn new() -> Self {
        Self {
            ingress: Mutex::new(None),
            dispose_count: AtomicUsize::new(0),
        }
    }

    fn ingress(&self) -> Arc<BackendEventIngressHandle> {
        self.ingress
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
            .expect("consumer backend ingress was not bound")
    }

    fn reject() -> Result<(), CommandDispatchError> {
        Err(CommandDispatchError::InvalidState {
            detail: "artifact consumer backend does not implement this command".to_owned(),
        })
    }
}

impl BleBackend for ConsumerBackend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        Ok(BackendCapabilities::new(Platform::Test))
    }

    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError> {
        let mut slot = self
            .ingress
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        if slot.is_some() {
            return Err(BleHubError::BackendAssembly {
                detail: "artifact consumer ingress was already bound".to_owned(),
            });
        }
        *slot = Some(ingress);
        Ok(())
    }

    fn initialize(&self, _: InitializeCommand) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        let ingress = self.ingress();
        assert_eq!(
            ingress.push(BleEvent::ScanStarted {
                command_id: command.command_id,
                scan_id: command.scan_session_id,
            }),
            EventIngressStatus::Delivered
        );
        assert_eq!(
            ingress.push(BleEvent::CommandCompleted {
                command_id: command.command_id,
                error: None,
            }),
            EventIngressStatus::Delivered
        );
        Ok(())
    }

    fn stop_scan(&self, _: StopScanCommand) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn connect(&self, _: ConnectCommand) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn disconnect(&self, _: DisconnectCommand) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn discover_gatt(&self, _: DiscoverGattCommand) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn read_characteristic(
        &self,
        _: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn write_characteristic(
        &self,
        _: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn set_notifications(&self, _: SetNotificationsCommand) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn read_rssi(&self, _: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        Self::reject()
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        self.ingress
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take();
        self.dispose_count.fetch_add(1, Ordering::AcqRel);
        Ok(())
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let events = Arc::new(Mutex::new(Vec::new()));
    let backend = Arc::new(ConsumerBackend::new());
    let hub = BleHub::new(
        backend.clone(),
        Box::new(RecordingSink {
            events: Arc::clone(&events),
        }),
    )?;
    let late_ingress = backend.ingress();
    let command = StartScanCommand {
        command_id: CommandId { value: 1 },
        scan_session_id: ScanSessionId { value: 1 },
        options: ScanOptions::default(),
    };

    hub.start_scan(command.clone())?;
    assert_eq!(
        *events.lock().unwrap_or_else(|error| error.into_inner()),
        vec![
            BleEvent::ScanStarted {
                command_id: command.command_id,
                scan_id: command.scan_session_id,
            },
            BleEvent::CommandCompleted {
                command_id: command.command_id,
                error: None,
            },
        ]
    );
    hub.dispose()?;
    hub.dispose()?;
    assert_eq!(backend.dispose_count.load(Ordering::Acquire), 1);
    assert_eq!(
        late_ingress.push(BleEvent::CommandCompleted {
            command_id: command.command_id,
            error: None,
        }),
        EventIngressStatus::Closed
    );

    let windows_backend = Arc::new(WindowsBackend::new()?);
    let windows_hub = BleHub::new(
        windows_backend,
        Box::new(RecordingSink {
            events: Arc::new(Mutex::new(Vec::new())),
        }),
    )?;
    assert_eq!(windows_hub.capabilities().platform, Platform::Windows);
    windows_hub.dispose()?;
    windows_hub.dispose()?;

    let replacement = WindowsBackend::new()?;
    drop(replacement);

    println!("Windows artifact-only consumer: PASS");
    Ok(())
}
