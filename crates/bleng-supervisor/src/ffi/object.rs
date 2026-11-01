use std::sync::{Arc, Mutex};

use super::adapter::{
    ForeignPlatformOwnerSessionFactory, ForeignPlatformOwnerSessionFactoryAdapter,
};
use super::types::*;
use crate::SupervisorConfig;
use crate::recovery::{RecoveryContext, RecoveryObservation};
/// Public lifecycle facade for one Rust recovery supervisor.
#[derive(uniffi::Object)]
pub struct Supervisor {
    inner: Mutex<Option<crate::SessionSupervisor>>,
}

#[uniffi::export]
impl Supervisor {
    #[uniffi::constructor]
    pub fn new(
        factory: Arc<dyn ForeignPlatformOwnerSessionFactory>,
        config: ForeignSupervisorConfig,
    ) -> Result<Arc<Self>, SupervisorFfiError> {
        let config: SupervisorConfig = config.try_into()?;
        let factory = Arc::new(ForeignPlatformOwnerSessionFactoryAdapter::new(factory));
        Ok(Arc::new(Self {
            inner: Mutex::new(Some(
                crate::SessionSupervisor::new(factory, config)
                    .map_err(SupervisorFfiError::from_supervisor)?,
            )),
        }))
    }

    pub fn is_active(&self) -> Result<bool, SupervisorFfiError> {
        Ok(self
            .lock_inner()?
            .as_ref()
            .is_some_and(crate::SessionSupervisor::is_active))
    }

    pub async fn start(&self) -> Result<(), SupervisorFfiError> {
        let mut supervisor = self.take_inner()?;
        let result = supervisor
            .start()
            .await
            .map_err(SupervisorFfiError::from_supervisor);
        self.restore_inner(supervisor)?;
        result
    }

    pub async fn execute_platform_action(&self) -> Result<ForeignRecoveryPlan, SupervisorFfiError> {
        let mut supervisor = self.take_inner()?;
        let result = supervisor
            .execute_platform_action()
            .await
            .map(ForeignRecoveryPlan::from)
            .map_err(SupervisorFfiError::from_supervisor);
        self.restore_inner(supervisor)?;
        result
    }

    pub async fn retire_for_recovery(&self) -> Result<ForeignRecoveryPlan, SupervisorFfiError> {
        let mut supervisor = self.take_inner()?;
        let result = supervisor
            .retire_for_recovery()
            .await
            .map(ForeignRecoveryPlan::from)
            .map_err(SupervisorFfiError::from_supervisor);
        self.restore_inner(supervisor)?;
        result
    }

    pub async fn create_fresh_owner(
        &self,
        context: ForeignOwnerContext,
    ) -> Result<ForeignRecoveryPlan, SupervisorFfiError> {
        let context: RecoveryContext = context.try_into()?;
        let mut supervisor = self.take_inner()?;
        let result = supervisor
            .create_fresh_owner(context)
            .await
            .map(ForeignRecoveryPlan::from)
            .map_err(SupervisorFfiError::from_supervisor);
        self.restore_inner(supervisor)?;
        result
    }

    pub async fn shutdown(&self) -> Result<Vec<ForeignResourceCount>, SupervisorFfiError> {
        let mut supervisor = self.take_inner()?;
        let result = supervisor
            .shutdown()
            .await
            .map(|snapshot| {
                snapshot
                    .into_iter()
                    .map(|(name, count)| ForeignResourceCount { name, count })
                    .collect()
            })
            .map_err(SupervisorFfiError::from_supervisor);
        self.restore_inner(supervisor)?;
        result
    }

    pub fn observe(
        &self,
        observation: ForeignRecoveryObservation,
    ) -> Result<ForeignRecoveryPlan, SupervisorFfiError> {
        let mut inner = self.lock_inner()?;
        let supervisor = inner.as_mut().ok_or_else(|| SupervisorFfiError::Platform {
            detail: "supervisor operation is already in progress".to_owned(),
        })?;
        let observation: RecoveryObservation = observation.try_into()?;
        supervisor
            .observe(observation)
            .map(ForeignRecoveryPlan::from)
            .map_err(SupervisorFfiError::from_supervisor)
    }
}

impl Supervisor {
    fn lock_inner(
        &self,
    ) -> Result<std::sync::MutexGuard<'_, Option<crate::SessionSupervisor>>, SupervisorFfiError>
    {
        self.inner.lock().map_err(|_| SupervisorFfiError::Platform {
            detail: "supervisor mutex is poisoned".to_owned(),
        })
    }

    fn take_inner(&self) -> Result<crate::SessionSupervisor, SupervisorFfiError> {
        self.lock_inner()?
            .take()
            .ok_or_else(|| SupervisorFfiError::Platform {
                detail: "supervisor operation is already in progress".to_owned(),
            })
    }

    fn restore_inner(
        &self,
        supervisor: crate::SessionSupervisor,
    ) -> Result<(), SupervisorFfiError> {
        let mut inner = self.lock_inner()?;
        *inner = Some(supervisor);
        Ok(())
    }
}
