use super::*;

pub(crate) struct BeginAssociateCompanion {
    pub(crate) submission: OperationSubmission,
    pub(crate) service_uuids: Vec<BluetoothUuid>,
    pub(crate) display_name: Option<String>,
    pub(crate) device_profile: Option<String>,
    pub(crate) notify_on_device_nearby: bool,
}

impl OperationSpec for BeginAssociateCompanion {
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
            WaiterSuccess::CompanionAssociationRequested => Some(()),
            _ => None,
        }
    }
}

impl Message<BeginAssociateCompanion> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginAssociateCompanion,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.require_companion_association_available()?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::CompanionAssociation,
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_companion_association(command_id);
                },
                move |hub| {
                    hub.associate_companion_device(AssociateCompanionDeviceCommand {
                        command_id,
                        service_uuids: message.service_uuids,
                        display_name: message.display_name,
                        device_profile: message.device_profile,
                        notify_on_device_nearby: message.notify_on_device_nearby,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}
