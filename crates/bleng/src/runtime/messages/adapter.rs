use super::*;

pub(crate) struct BeginInitialize {
    pub(crate) submission: OperationSubmission,
}

pub(crate) struct BeginSetAdapterEnabled {
    pub(crate) submission: OperationSubmission,
    pub(crate) enabled: bool,
}

pub(crate) struct BeginQueryDevices {
    pub(crate) submission: OperationSubmission,
    pub(crate) kind: DeviceQueryKind,
    pub(crate) service_uuids: Vec<BluetoothUuid>,
}

impl OperationSpec for BeginInitialize {
    type Resource = ();
    type Output = AdapterSnapshot;
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        (): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::Initialized(snapshot) => Some(snapshot),
            _ => None,
        }
    }
}

impl OperationSpec for BeginSetAdapterEnabled {
    type Resource = ();
    type Output = ();
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        (): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::AdapterCommandCompleted => Some(()),
            _ => None,
        }
    }
}

impl OperationSpec for BeginQueryDevices {
    type Resource = ();
    type Output = Vec<blehub::DeviceRecord>;
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        (): Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::DevicesListed(devices) => Some(devices),
            _ => None,
        }
    }
}

impl Message<BeginInitialize> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginInitialize,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_not_started()?;
            let command_id = self
                .register_operation_waiter(&message.submission, WaiterExpectation::Initialized)?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_initialize(command_id);
                },
                move |hub| hub.initialize(InitializeCommand { command_id }),
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginSetAdapterEnabled> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginSetAdapterEnabled,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.require_adapter_available()?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::AdapterControl,
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_adapter_control(command_id);
                },
                move |hub| {
                    hub.set_adapter_enabled(SetAdapterEnabledCommand {
                        command_id,
                        enabled: message.enabled,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}

impl Message<BeginQueryDevices> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginQueryDevices,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.require_devices_query_available()?;
            let command_id = self
                .register_operation_waiter(&message.submission, WaiterExpectation::DevicesListed)?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_device_query(command_id);
                },
                move |hub| {
                    hub.query_devices(QueryDevicesCommand {
                        command_id,
                        kind: message.kind,
                        service_uuids: message.service_uuids,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}
