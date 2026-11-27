use super::*;

pub(crate) struct BeginConnect {
    pub(crate) submission: OperationSubmission<ConnectionId>,
    pub(crate) peripheral_id: PeripheralId,
    pub(crate) auto_connect: bool,
}

pub(crate) struct BeginDisconnect {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
}

pub(crate) struct BeginDiscoverGatt {
    pub(crate) submission: OperationSubmission<GattSessionId>,
    pub(crate) connection: ConnectionId,
}

pub(crate) struct BeginRssi {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
}

pub(crate) struct BeginMtu {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) request: Option<u16>,
}

pub(crate) struct BeginSetConnectionPriority {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) priority: ConnectionPriority,
}

pub(crate) struct BeginCreateBond {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) pin: Option<String>,
}

pub(crate) struct BeginRemoveBond {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
}

pub(crate) struct BeginRefreshGattCache {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
}

impl OperationSpec for BeginConnect {
    type Resource = ConnectionId;
    type Output = ConnectionId;
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        (): Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::Connected(found) if found == resource => Some(found),
            _ => None,
        }
    }
}

impl OperationSpec for BeginDisconnect {
    type Resource = ();
    type Output = ();
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::Disconnected(found) if found == connection => Some(()),
            _ => None,
        }
    }
}

impl OperationSpec for BeginDiscoverGatt {
    type Resource = GattSessionId;
    type Output = GattDatabase;
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::GattDiscovered(database)
                if database.connection_id == connection && database.gatt_session_id == resource =>
            {
                Some(database)
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginRssi {
    type Resource = ();
    type Output = i16;
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::RssiRead(found_connection, value) if found_connection == connection => {
                Some(value)
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginMtu {
    type Resource = ();
    type Output = MtuInfo;
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::MtuRead(found_connection, info) if found_connection == connection => {
                Some(info)
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginSetConnectionPriority {
    type Resource = ();
    type Output = ();
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::ConnectionPriorityAccepted(found_connection)
                if found_connection == connection =>
            {
                Some(())
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginCreateBond {
    type Resource = ();
    type Output = BondState;
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::BondState(found_connection, state) if found_connection == connection => {
                Some(state)
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginRemoveBond {
    type Resource = ();
    type Output = BondState;
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::BondState(found_connection, state) if found_connection == connection => {
                Some(state)
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginRefreshGattCache {
    type Resource = ();
    type Output = ();
    type Correlation = ConnectionId;

    fn correlation(&self) -> Self::Correlation {
        self.connection
    }

    fn decode_success(
        connection: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::GattCacheRefreshed(found_connection)
                if found_connection == connection =>
            {
                Some(())
            }
            _ => None,
        }
    }
}

impl Message<BeginConnect> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginConnect,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.require_peripheral_available(message.peripheral_id)?;
            let connection_id: ConnectionId = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::Connected(connection_id),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_connection(
                        message.peripheral_id,
                        command_id,
                        connection_id,
                    );
                },
                move |hub| {
                    hub.connect(ConnectCommand {
                        command_id,
                        connection_id,
                        peripheral_id: message.peripheral_id,
                        auto_connect: message.auto_connect,
                    })
                },
            )?;
            Ok(connection_id)
        })();
        submission.resolve(result);
    }
}

impl Message<BeginDisconnect> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginDisconnect,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_disconnect(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::Disconnected(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_disconnect(peripheral_id, command_id);
                },
                move |hub| {
                    hub.disconnect(DisconnectCommand {
                        command_id,
                        connection_id: message.connection,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginDiscoverGatt> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginDiscoverGatt,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_gatt_discovery(message.connection)?;
            let session_id: GattSessionId = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::GattDiscovered(message.connection, session_id),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_gatt_discovery(peripheral_id, command_id, session_id);
                },
                move |hub| {
                    hub.discover_gatt(DiscoverGattCommand {
                        command_id,
                        connection_id: message.connection,
                        gatt_session_id: session_id,
                    })
                },
            )?;
            Ok(session_id)
        })();
        submission.resolve(result);
    }
}

impl Message<BeginRssi> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginRssi,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_rssi(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::RssiRead(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_rssi(peripheral_id, command_id);
                },
                move |hub| {
                    hub.read_rssi(ReadRssiCommand {
                        command_id,
                        connection_id: message.connection,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginMtu> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginMtu,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_mtu(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::MtuRead(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_mtu(peripheral_id, command_id);
                },
                move |hub| {
                    hub.mtu(MtuCommand {
                        command_id,
                        connection_id: message.connection,
                        request: message.request,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginSetConnectionPriority> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginSetConnectionPriority,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_set_connection_priority(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::ConnectionPriority(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_set_connection_priority(peripheral_id, command_id);
                },
                move |hub| {
                    hub.set_connection_priority(SetConnectionPriorityCommand {
                        command_id,
                        connection_id: message.connection,
                        priority: message.priority,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginCreateBond> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginCreateBond,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_create_bond(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::BondState(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_create_bond(peripheral_id, command_id);
                },
                move |hub| {
                    hub.create_bond(CreateBondCommand {
                        command_id,
                        connection_id: message.connection,
                        pin: message.pin,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginRemoveBond> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginRemoveBond,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_remove_bond(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::BondRemoval(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_remove_bond(peripheral_id, command_id);
                },
                move |hub| {
                    hub.remove_bond(RemoveBondCommand {
                        command_id,
                        connection_id: message.connection,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginRefreshGattCache> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginRefreshGattCache,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_refresh_gatt_cache(message.connection)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::GattCacheRefreshed(message.connection),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_refresh_gatt_cache(peripheral_id, command_id);
                },
                move |hub| {
                    hub.refresh_gatt_cache(RefreshGattCacheCommand {
                        command_id,
                        connection_id: message.connection,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}
