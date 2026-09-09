use std::sync::{Arc, Mutex};

use blehub::{
    AdapterSnapshot, AdapterState, AdvertisementObservation, AuthorizationState,
    BackendCapabilities, BackendEventIngressHandle, BleBackend, BleErrorCategory, BleEvent,
    BleEventError, BleHubError, BluetoothUuid, CharacteristicProperties, CommandDispatchError,
    ConnectionState, ConnectionStateResult, GattCharacteristic, GattDatabase, GattDescriptor,
    GattService, InitializeCommand, NotificationAction, NotificationMode, NotificationModeRequest,
    NotificationState, Platform, ReadCharacteristicCommand, ReadRssiCommand, ScanStopReason,
    ScanStopResult, SetNotificationsCommand, StartScanCommand, StopScanCommand,
    WriteCharacteristicCommand,
};

const PERIPHERAL_ID: u64 = 201;
const SERVICE_UUID: &str = "fff00000-0000-4000-8000-000000000000";
const CHARACTERISTIC_UUID: &str = "fff10000-0000-4000-8000-000000000000";
const NOTIFICATION_CHARACTERISTIC_UUID: &str = "fff20000-0000-4000-8000-000000000000";

/// In-process UI demonstration only, not a BLE implementation or conformance fixture.
pub struct DemoBackend {
    ingress: Mutex<Option<Arc<BackendEventIngressHandle>>>,
    value: Mutex<Vec<u8>>,
}

impl DemoBackend {
    pub fn new() -> Self {
        Self {
            ingress: Mutex::new(None),
            value: Mutex::new(vec![0x00]),
        }
    }

    fn emit(&self, event: BleEvent) {
        let ingress = self
            .ingress
            .lock()
            .expect("demo backend ingress mutex")
            .clone();
        if let Some(ingress) = ingress {
            let _ = ingress.push(event);
        }
    }

    fn complete(&self, command_id: blehub::CommandId) {
        self.emit(BleEvent::CommandCompleted {
            command_id,
            error: None,
        });
    }

    fn fail(&self, command_id: blehub::CommandId, message: &str) {
        self.emit(BleEvent::CommandCompleted {
            command_id,
            error: Some(BleEventError {
                category: BleErrorCategory::Gatt,
                message: message.to_owned(),
                platform_details: None,
            }),
        });
    }

    fn database(command: &blehub::DiscoverGattCommand) -> GattDatabase {
        GattDatabase {
            connection_id: command.connection_id,
            gatt_session_id: command.gatt_session_id,
            services: vec![GattService {
                id: blehub::GattServiceId { value: 501 },
                uuid: uuid(SERVICE_UUID),
                primary: Some(true),
                characteristics: vec![
                    GattCharacteristic {
                        id: blehub::GattCharacteristicId { value: 601 },
                        uuid: uuid(CHARACTERISTIC_UUID),
                        properties: CharacteristicProperties {
                            readable: true,
                            writable_with_response: true,
                            writable_without_response: true,
                            notifiable: false,
                            indicatable: false,
                        },
                        descriptors: Vec::new(),
                    },
                    GattCharacteristic {
                        id: blehub::GattCharacteristicId { value: 602 },
                        uuid: uuid(NOTIFICATION_CHARACTERISTIC_UUID),
                        properties: CharacteristicProperties {
                            readable: true,
                            writable_with_response: false,
                            writable_without_response: false,
                            notifiable: true,
                            indicatable: true,
                        },
                        descriptors: vec![GattDescriptor {
                            id: blehub::GattDescriptorId { value: 702 },
                            uuid: uuid("00002902-0000-1000-8000-00805f9b34fb"),
                        }],
                    },
                ],
            }],
        }
    }
}

impl Default for DemoBackend {
    fn default() -> Self {
        Self::new()
    }
}

impl BleBackend for DemoBackend {
    fn capabilities(&self) -> Result<BackendCapabilities, BleHubError> {
        let mut capabilities = BackendCapabilities::new(Platform::Test);
        capabilities.supports_duplicate_scan_results = true;
        capabilities.supports_connectable_flag = true;
        capabilities.supports_read_rssi = true;
        capabilities.supports_required_notification = true;
        capabilities.supports_required_indication = true;
        capabilities.reports_actual_notification_mode = true;
        Ok(capabilities)
    }

    fn bind_event_ingress(
        &self,
        ingress: Arc<BackendEventIngressHandle>,
    ) -> Result<(), BleHubError> {
        let mut current = self.ingress.lock().expect("demo backend ingress mutex");
        if current.is_some() {
            return Err(BleHubError::BackendAssembly {
                detail: "demo backend event ingress is already bound".to_owned(),
            });
        }
        *current = Some(ingress);
        Ok(())
    }

    fn initialize(&self, command: InitializeCommand) -> Result<(), CommandDispatchError> {
        self.emit(BleEvent::AdapterSnapshotChanged {
            command_id: Some(command.command_id),
            snapshot: AdapterSnapshot {
                state: AdapterState::PoweredOn,
                scan_authorization: AuthorizationState::Authorized,
                connection_authorization: AuthorizationState::Authorized,
                name: Some("tauri-central-demo".to_owned()),
            },
        });
        self.complete(command.command_id);
        Ok(())
    }

    fn start_scan(&self, command: StartScanCommand) -> Result<(), CommandDispatchError> {
        self.emit(BleEvent::ScanStarted {
            command_id: command.command_id,
            scan_id: command.scan_session_id,
        });
        let advertisement = AdvertisementObservation {
            scan_session_id: command.scan_session_id,
            peripheral_id: blehub::PeripheralId {
                value: PERIPHERAL_ID,
            },
            local_name: Some("BleHub Demo Peripheral".to_owned()),
            advertised_services: vec![uuid(SERVICE_UUID)],
            manufacturer_data: Vec::new(),
            service_data: Vec::new(),
            rssi: Some(-48),
            connectable: Some(true),
        };
        if command.options.matches(&advertisement) {
            self.emit(BleEvent::AdvertisementObserved { advertisement });
        }
        self.complete(command.command_id);
        Ok(())
    }

    fn stop_scan(&self, command: StopScanCommand) -> Result<(), CommandDispatchError> {
        self.emit(BleEvent::ScanStopped {
            scan_id: command.scan_session_id,
            result: ScanStopResult::Ok {
                reason: ScanStopReason::Requested {
                    command_id: command.command_id,
                },
            },
        });
        self.complete(command.command_id);
        Ok(())
    }

    fn connect(&self, command: blehub::ConnectCommand) -> Result<(), CommandDispatchError> {
        self.emit(BleEvent::ConnectionStateChanged {
            command_id: Some(command.command_id),
            connection_id: command.connection_id,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Connected,
            },
        });
        self.complete(command.command_id);
        Ok(())
    }

    fn disconnect(&self, command: blehub::DisconnectCommand) -> Result<(), CommandDispatchError> {
        self.emit(BleEvent::ConnectionStateChanged {
            command_id: Some(command.command_id),
            connection_id: command.connection_id,
            result: ConnectionStateResult::Ok {
                state: ConnectionState::Disconnected,
            },
        });
        self.complete(command.command_id);
        Ok(())
    }

    fn discover_gatt(
        &self,
        command: blehub::DiscoverGattCommand,
    ) -> Result<(), CommandDispatchError> {
        self.emit(BleEvent::GattDiscovered {
            command_id: command.command_id,
            database: Self::database(&command),
        });
        self.complete(command.command_id);
        Ok(())
    }

    fn read_characteristic(
        &self,
        command: ReadCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        let value = match command.characteristic_id.value {
            601 => self.value.lock().expect("demo value mutex").clone(),
            602 => vec![0xde, 0xad, 0xbe, 0xef],
            _ => {
                self.fail(command.command_id, "unknown demo characteristic");
                return Ok(());
            }
        };
        self.emit(BleEvent::CharacteristicRead {
            command_id: command.command_id,
            connection_id: command.connection_id,
            gatt_session_id: command.gatt_session_id,
            characteristic_id: command.characteristic_id,
            value,
        });
        self.complete(command.command_id);
        Ok(())
    }

    fn write_characteristic(
        &self,
        command: WriteCharacteristicCommand,
    ) -> Result<(), CommandDispatchError> {
        if command.characteristic_id.value != 601 {
            self.fail(command.command_id, "demo characteristic is not writable");
            return Ok(());
        }
        *self.value.lock().expect("demo value mutex") = command.value;
        self.emit(BleEvent::CharacteristicWritten {
            command_id: command.command_id,
            connection_id: command.connection_id,
            gatt_session_id: command.gatt_session_id,
            characteristic_id: command.characteristic_id,
        });
        self.complete(command.command_id);
        Ok(())
    }

    fn set_notifications(
        &self,
        command: SetNotificationsCommand,
    ) -> Result<(), CommandDispatchError> {
        if command.characteristic_id.value != 602 {
            self.fail(
                command.command_id,
                "demo characteristic does not support subscriptions",
            );
            return Ok(());
        }
        match command.action {
            NotificationAction::Enable { mode } => {
                let actual_mode = match mode {
                    NotificationModeRequest::PlatformDefault
                    | NotificationModeRequest::RequireNotification => {
                        NotificationMode::Notification
                    }
                    NotificationModeRequest::RequireIndication => NotificationMode::Indication,
                };
                self.emit(BleEvent::NotificationStateChanged {
                    command_id: command.command_id,
                    connection_id: command.connection_id,
                    gatt_session_id: command.gatt_session_id,
                    subscription_id: command.subscription_id,
                    characteristic_id: command.characteristic_id,
                    state: NotificationState::Enabled {
                        actual_mode: Some(actual_mode),
                    },
                });
                self.emit(BleEvent::NotificationReceived {
                    connection_id: command.connection_id,
                    gatt_session_id: command.gatt_session_id,
                    subscription_id: command.subscription_id,
                    characteristic_id: command.characteristic_id,
                    value: vec![0xde, 0xad, 0xbe, 0xef],
                });
            }
            NotificationAction::Disable => self.emit(BleEvent::NotificationStateChanged {
                command_id: command.command_id,
                connection_id: command.connection_id,
                gatt_session_id: command.gatt_session_id,
                subscription_id: command.subscription_id,
                characteristic_id: command.characteristic_id,
                state: NotificationState::Disabled,
            }),
        }
        self.complete(command.command_id);
        Ok(())
    }

    fn read_rssi(&self, command: ReadRssiCommand) -> Result<(), CommandDispatchError> {
        self.emit(BleEvent::RssiRead {
            command_id: command.command_id,
            connection_id: command.connection_id,
            rssi: -48,
        });
        self.complete(command.command_id);
        Ok(())
    }

    fn dispose(&self) -> Result<(), BleHubError> {
        self.ingress
            .lock()
            .expect("demo backend ingress mutex")
            .take();
        Ok(())
    }
}

fn uuid(value: &str) -> BluetoothUuid {
    BluetoothUuid {
        value: value.to_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use blehub::{BleEventSink, BleHub, EventSinkError};

    #[derive(Default)]
    struct Sink(Mutex<Vec<BleEvent>>);

    impl BleEventSink for Sink {
        fn on_event(&self, event: BleEvent) -> Result<(), EventSinkError> {
            self.0.lock().unwrap().push(event);
            Ok(())
        }
    }

    #[test]
    fn demo_readback_tracks_both_write_types_and_rejects_non_writable_characteristics() {
        let sink = Arc::new(Sink::default());
        let hub = BleHub::new(Arc::new(DemoBackend::new()), sink.clone()).unwrap();
        for (index, write_type) in [
            blehub::WriteType::WithResponse,
            blehub::WriteType::WithoutResponse,
        ]
        .into_iter()
        .enumerate()
        {
            let value = vec![index as u8, 0xab, 0xcd];
            hub.write_characteristic(WriteCharacteristicCommand {
                command_id: blehub::CommandId {
                    value: 10 + index as u64 * 2,
                },
                connection_id: blehub::ConnectionId { value: 1 },
                gatt_session_id: blehub::GattSessionId { value: 2 },
                characteristic_id: blehub::GattCharacteristicId { value: 601 },
                value: value.clone(),
                write_type,
            })
            .unwrap();
            hub.read_characteristic(ReadCharacteristicCommand {
                command_id: blehub::CommandId {
                    value: 11 + index as u64 * 2,
                },
                connection_id: blehub::ConnectionId { value: 1 },
                gatt_session_id: blehub::GattSessionId { value: 2 },
                characteristic_id: blehub::GattCharacteristicId { value: 601 },
            })
            .unwrap();
            let events = std::mem::take(&mut *sink.0.lock().unwrap());
            assert_eq!(events.len(), 4);
            assert!(
                matches!(&events[2], BleEvent::CharacteristicRead { value: actual, .. } if actual == &value)
            );
            assert!(matches!(
                &events[3],
                BleEvent::CommandCompleted { error: None, .. }
            ));
        }
        hub.write_characteristic(WriteCharacteristicCommand {
            command_id: blehub::CommandId { value: 20 },
            connection_id: blehub::ConnectionId { value: 1 },
            gatt_session_id: blehub::GattSessionId { value: 2 },
            characteristic_id: blehub::GattCharacteristicId { value: 602 },
            value: vec![1],
            write_type: blehub::WriteType::WithResponse,
        })
        .unwrap();
        let events = std::mem::take(&mut *sink.0.lock().unwrap());
        assert!(matches!(
            events.as_slice(),
            [BleEvent::CommandCompleted { error: Some(_), .. }]
        ));
        hub.dispose().unwrap();
    }

    #[test]
    fn demo_reports_requested_strict_mode_and_disable_emits_no_value() {
        let sink = Arc::new(Sink::default());
        let hub = BleHub::new(Arc::new(DemoBackend::new()), sink.clone()).unwrap();
        for (index, (mode, expected)) in [
            (
                NotificationModeRequest::PlatformDefault,
                NotificationMode::Notification,
            ),
            (
                NotificationModeRequest::RequireNotification,
                NotificationMode::Notification,
            ),
            (
                NotificationModeRequest::RequireIndication,
                NotificationMode::Indication,
            ),
        ]
        .into_iter()
        .enumerate()
        {
            let command = SetNotificationsCommand {
                command_id: blehub::CommandId {
                    value: 30 + index as u64 * 2,
                },
                connection_id: blehub::ConnectionId { value: 1 },
                gatt_session_id: blehub::GattSessionId { value: 2 },
                subscription_id: blehub::SubscriptionId {
                    value: 40 + index as u64,
                },
                characteristic_id: blehub::GattCharacteristicId { value: 602 },
                action: NotificationAction::Enable { mode },
            };
            hub.set_notifications(command).unwrap();
            let events = std::mem::take(&mut *sink.0.lock().unwrap());
            assert!(matches!(events.as_slice(), [
                BleEvent::NotificationStateChanged { state: NotificationState::Enabled { actual_mode: Some(actual) }, .. },
                BleEvent::NotificationReceived { .. },
                BleEvent::CommandCompleted { error: None, .. },
            ] if *actual == expected));
            hub.set_notifications(SetNotificationsCommand {
                command_id: blehub::CommandId {
                    value: command.command_id.value + 1,
                },
                action: NotificationAction::Disable,
                ..command
            })
            .unwrap();
            let events = std::mem::take(&mut *sink.0.lock().unwrap());
            assert!(matches!(
                events.as_slice(),
                [
                    BleEvent::NotificationStateChanged {
                        state: NotificationState::Disabled,
                        ..
                    },
                    BleEvent::CommandCompleted { error: None, .. },
                ]
            ));
        }
        hub.dispose().unwrap();
    }
}
