use super::*;

pub(crate) struct BeginScan {
    pub(crate) submission: OperationSubmission<ScanSessionId>,
    pub(crate) options: ScanOptions,
}

pub(crate) struct BeginStop {
    pub(crate) submission: OperationSubmission,
    pub(crate) session: ScanSessionId,
}

impl OperationSpec for BeginScan {
    type Resource = ScanSessionId;
    type Output = ScanSessionId;
    type Correlation = ();

    fn correlation(&self) -> Self::Correlation {}

    fn decode_success(
        (): Self::Correlation,
        resource: Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::ScanStarted(scan) if scan == resource => Some(scan),
            _ => None,
        }
    }
}

impl OperationSpec for BeginStop {
    type Resource = ();
    type Output = ();
    type Correlation = ScanSessionId;

    fn correlation(&self) -> Self::Correlation {
        self.session
    }

    fn decode_success(
        session: Self::Correlation,
        (): Self::Resource,
        success: WaiterSuccess,
    ) -> Option<Self::Output> {
        match success {
            WaiterSuccess::ScanStopped(stopped) if stopped == session => Some(()),
            _ => None,
        }
    }
}

impl Message<BeginScan> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginScan,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.require_scan_available()?;
            let session_id: ScanSessionId = self.allocate_resource_id()?.into();
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::ScanStarted(session_id),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_scan_start(command_id, session_id);
                },
                move |hub| {
                    hub.start_scan(StartScanCommand {
                        command_id,
                        scan_session_id: session_id,
                        options: message.options,
                    })
                },
            )?;
            Ok(session_id)
        })();
        submission.resolve(result);
    }
}

impl Message<BeginStop> for BlengActor {
    type Reply = ();

    async fn handle(
        &mut self,
        message: BeginStop,
        ctx: &mut Context<Self, Self::Reply>,
    ) -> Self::Reply {
        let submission = message.submission.clone();
        let result = (|| {
            self.require_open()?;
            self.require_initialized()?;
            self.validate_scan_stop(message.session)?;
            let command_id = self.register_operation_waiter(
                &message.submission,
                WaiterExpectation::ScanStopped(message.session),
            )?;
            self.launch_dispatch(
                ctx,
                &message.submission,
                command_id,
                |reservations| {
                    reservations.reserve_scan_stop(command_id);
                },
                move |hub| {
                    hub.stop_scan(StopScanCommand {
                        command_id,
                        scan_session_id: message.session,
                    })
                },
            )?;
            Ok(())
        })();
        submission.resolve(result);
    }
}
