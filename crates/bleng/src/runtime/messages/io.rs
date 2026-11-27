use super::*;

pub(crate) struct BeginRead {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) characteristic: GattCharacteristicId,
}

pub(crate) struct BeginWrite {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) characteristic: GattCharacteristicId,
    pub(crate) value: Vec<u8>,
    pub(crate) write_type: WriteType,
}

pub(crate) struct BeginReadDescriptor {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) characteristic: GattCharacteristicId,
    pub(crate) descriptor: GattDescriptorId,
}

pub(crate) struct BeginWriteDescriptor {
    pub(crate) submission: OperationSubmission,
    pub(crate) connection: ConnectionId,
    pub(crate) session: GattSessionId,
    pub(crate) characteristic: GattCharacteristicId,
    pub(crate) descriptor: GattDescriptorId,
    pub(crate) value: Vec<u8>,
}

impl OperationSpec for BeginRead {
    type Resource = ();
    type Output = Vec<u8>;
    type Correlation = (ConnectionId, GattSessionId, GattCharacteristicId);

    fn correlation(&self) -> Self::Correlation {
        (self.connection, self.session, self.characteristic)
    }

    fn decode_success(
        (connection, session, characteristic): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::CharacteristicRead {
                connection_id,
                gatt_session_id,
                characteristic_id,
                value,
            } if connection_id == connection
                && gatt_session_id == session
                && characteristic_id == characteristic =>
            {
                Some(value)
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginWrite {
    type Resource = ();
    type Output = ();
    type Correlation = (ConnectionId, GattSessionId, GattCharacteristicId);

    fn correlation(&self) -> Self::Correlation {
        (self.connection, self.session, self.characteristic)
    }

    fn decode_success(
        (connection, session, characteristic): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::CharacteristicWritten(
                found_connection,
                found_session,
                found_characteristic,
            ) if found_connection == connection
                && found_session == session
                && found_characteristic == characteristic =>
            {
                Some(())
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginReadDescriptor {
    type Resource = ();
    type Output = Vec<u8>;
    type Correlation = (
        ConnectionId,
        GattSessionId,
        GattCharacteristicId,
        GattDescriptorId,
    );

    fn correlation(&self) -> Self::Correlation {
        (
            self.connection,
            self.session,
            self.characteristic,
            self.descriptor,
        )
    }

    fn decode_success(
        (connection, session, characteristic, descriptor): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::DescriptorRead {
                connection_id,
                gatt_session_id,
                characteristic_id,
                descriptor_id,
                value,
            } if connection_id == connection
                && gatt_session_id == session
                && characteristic_id == characteristic
                && descriptor_id == descriptor =>
            {
                Some(value)
            }
            _ => None,
        }
    }
}

impl OperationSpec for BeginWriteDescriptor {
    type Resource = ();
    type Output = ();
    type Correlation = (
        ConnectionId,
        GattSessionId,
        GattCharacteristicId,
        GattDescriptorId,
    );

    fn correlation(&self) -> Self::Correlation {
        (
            self.connection,
            self.session,
            self.characteristic,
            self.descriptor,
        )
    }

    fn decode_success(
        (connection, session, characteristic, descriptor): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::DescriptorWritten {
                connection_id,
                gatt_session_id,
                characteristic_id,
                descriptor_id,
            } if connection_id == connection
                && gatt_session_id == session
                && characteristic_id == characteristic
                && descriptor_id == descriptor =>
            {
                Some(())
            }
            _ => None,
        }
    }
}

impl Message<BeginRead> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginRead,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_gatt_access(
                message.connection,
                message.session,
                message.characteristic,
                GattAccess::Read,
            )?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::CharacteristicRead(
                    message.connection,
                    message.session,
                    message.characteristic,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_read(
                        peripheral_id,
                        command_id,
                        message.session,
                        message.characteristic,
                    );
                },
                move |hub| {
                    hub.read_characteristic(ReadCharacteristicCommand {
                        command_id,
                        connection_id: message.connection,
                        gatt_session_id: message.session,
                        characteristic_id: message.characteristic,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginWrite> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginWrite,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_gatt_access(
                message.connection,
                message.session,
                message.characteristic,
                GattAccess::Write,
            )?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::CharacteristicWritten(
                    message.connection,
                    message.session,
                    message.characteristic,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_write(
                        peripheral_id,
                        command_id,
                        message.session,
                        message.characteristic,
                    );
                },
                move |hub| {
                    hub.write_characteristic(WriteCharacteristicCommand {
                        command_id,
                        connection_id: message.connection,
                        gatt_session_id: message.session,
                        characteristic_id: message.characteristic,
                        value: message.value,
                        write_type: message.write_type,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginReadDescriptor> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginReadDescriptor,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_descriptor_access(
                message.connection,
                message.session,
                message.characteristic,
                message.descriptor,
            )?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::DescriptorRead(
                    message.connection,
                    message.session,
                    message.characteristic,
                    message.descriptor,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_descriptor_read(
                        peripheral_id,
                        command_id,
                        message.session,
                        message.characteristic,
                        message.descriptor,
                    );
                },
                move |hub| {
                    hub.read_descriptor(ReadDescriptorCommand {
                        command_id,
                        connection_id: message.connection,
                        gatt_session_id: message.session,
                        characteristic_id: message.characteristic,
                        descriptor_id: message.descriptor,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginWriteDescriptor> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginWriteDescriptor,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            let peripheral_id = self.validate_descriptor_access(
                message.connection,
                message.session,
                message.characteristic,
                message.descriptor,
            )?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::DescriptorWritten(
                    message.connection,
                    message.session,
                    message.characteristic,
                    message.descriptor,
                ),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_descriptor_write(
                        peripheral_id,
                        command_id,
                        message.session,
                        message.characteristic,
                        message.descriptor,
                    );
                },
                move |hub| {
                    hub.write_descriptor(WriteDescriptorCommand {
                        command_id,
                        connection_id: message.connection,
                        gatt_session_id: message.session,
                        characteristic_id: message.characteristic,
                        descriptor_id: message.descriptor,
                        value: message.value,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}
