#[cfg(windows)]
mod windows_example {
    use std::{
        error::Error,
        io,
        sync::{Arc, mpsc},
        time::{Duration, Instant},
    };

    use blehub::{
        BleEvent, BleEventSink, BleHub, BluetoothUuid, CommandId, EventSinkError,
        InitializeCommand, ScanOptions, ScanSessionId, StartScanCommand, StopScanCommand,
    };
    use blehub_windows::WindowsBackend;

    type ExampleError = Box<dyn Error>;

    struct ChannelSink {
        sender: mpsc::Sender<BleEvent>,
    }

    impl BleEventSink for ChannelSink {
        fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
            self.sender.send(event).map_err(|error| EventSinkError {
                message: error.to_string(),
            })
        }
    }

    pub fn run() -> Result<(), ExampleError> {
        let (service_uuid, scan_duration) = parse_arguments()?;
        let (sender, receiver) = mpsc::channel();
        let backend = Arc::new(WindowsBackend::new()?);
        let hub = BleHub::new(backend, Box::new(ChannelSink { sender }))?;

        let capabilities = hub.capabilities();
        println!(
            "platform={:?} contract_version={}",
            capabilities.platform, capabilities.contract_version
        );

        let session_result = run_scan_session(&hub, &receiver, service_uuid, scan_duration);
        let first_dispose = hub.dispose();
        let second_dispose = hub.dispose();

        session_result?;
        first_dispose?;
        second_dispose?;
        Ok(())
    }

    fn run_scan_session(
        hub: &BleHub,
        receiver: &mpsc::Receiver<BleEvent>,
        service_uuid: BluetoothUuid,
        scan_duration: Duration,
    ) -> Result<(), ExampleError> {
        const COMMAND_TIMEOUT: Duration = Duration::from_secs(10);
        let initialize_id = CommandId { value: 1 };
        let start_id = CommandId { value: 2 };
        let stop_id = CommandId { value: 3 };
        let scan_session_id = ScanSessionId { value: 1 };

        hub.initialize(InitializeCommand {
            command_id: initialize_id,
        })?;
        wait_for_command(receiver, initialize_id, Instant::now() + COMMAND_TIMEOUT)?;

        hub.start_scan(StartScanCommand {
            command_id: start_id,
            scan_session_id,
            options: ScanOptions {
                service_uuids: vec![service_uuid],
                allow_duplicates: false,
                ..ScanOptions::default()
            },
        })?;
        wait_for_command(receiver, start_id, Instant::now() + COMMAND_TIMEOUT)?;

        let scan_deadline = Instant::now() + scan_duration;
        while Instant::now() < scan_deadline {
            match receiver.recv_timeout(scan_deadline.saturating_duration_since(Instant::now())) {
                Ok(event) => handle_event(event)?,
                Err(mpsc::RecvTimeoutError::Timeout) => break,
                Err(error) => return Err(error.into()),
            }
        }

        hub.stop_scan(StopScanCommand {
            command_id: stop_id,
            scan_session_id,
        })?;
        wait_for_command(receiver, stop_id, Instant::now() + COMMAND_TIMEOUT)
    }

    fn wait_for_command(
        receiver: &mpsc::Receiver<BleEvent>,
        expected_id: CommandId,
        deadline: Instant,
    ) -> Result<(), ExampleError> {
        loop {
            let event =
                receiver.recv_timeout(deadline.saturating_duration_since(Instant::now()))?;
            match &event {
                BleEvent::CommandCompleted { command_id, error } if *command_id == expected_id => {
                    handle_event(event.clone())?;
                    return match error {
                        None => Ok(()),
                        Some(error) => Err(io::Error::other(format!(
                            "command {} failed: {error}",
                            expected_id.value
                        ))
                        .into()),
                    };
                }
                _ => handle_event(event)?,
            }
        }
    }

    fn handle_event(event: BleEvent) -> Result<(), ExampleError> {
        match event {
            BleEvent::AdapterSnapshotChanged { snapshot, .. } => {
                println!(
                    "adapter state={:?} scan_authorization={:?}",
                    snapshot.state, snapshot.scan_authorization
                );
            }
            BleEvent::ScanStarted { scan_id, .. } => {
                println!("scan {} started", scan_id.value);
            }
            BleEvent::AdvertisementObserved { advertisement } => {
                println!(
                    "peripheral={} name={} rssi={:?} connectable={:?}",
                    advertisement.peripheral_id.value,
                    advertisement.local_name.as_deref().unwrap_or("<unnamed>"),
                    advertisement.rssi,
                    advertisement.connectable
                );
            }
            BleEvent::ScanStopped { scan_id, result } => {
                println!("scan {} stopped: {result:?}", scan_id.value);
            }
            BleEvent::CommandCompleted { command_id, error } => match error {
                None => println!("command {} completed", command_id.value),
                Some(error) => println!("command {} failed: {error}", command_id.value),
            },
            BleEvent::BackendError { error } => {
                return Err(io::Error::other(format!("backend error: {error}")).into());
            }
            other => println!("event: {other:?}"),
        }
        Ok(())
    }

    fn parse_arguments() -> Result<(BluetoothUuid, Duration), ExampleError> {
        let mut arguments = std::env::args();
        let executable = arguments
            .next()
            .unwrap_or_else(|| "blehub-windows-central-example".to_owned());
        let service = arguments.next().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("usage: {executable} <service-uuid> [scan-seconds]"),
            )
        })?;
        let service_uuid = BluetoothUuid::canonicalize(&service).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("invalid Bluetooth service UUID: {service}"),
            )
        })?;
        let scan_seconds = arguments
            .next()
            .map(|value| value.parse::<u64>())
            .transpose()?
            .unwrap_or(10);
        if scan_seconds == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "scan-seconds must be greater than zero",
            )
            .into());
        }
        if arguments.next().is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("usage: {executable} <service-uuid> [scan-seconds]"),
            )
            .into());
        }
        Ok((service_uuid, Duration::from_secs(scan_seconds)))
    }
}

#[cfg(windows)]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    windows_example::run()
}

#[cfg(not(windows))]
fn main() {
    eprintln!("this example requires Windows");
}
