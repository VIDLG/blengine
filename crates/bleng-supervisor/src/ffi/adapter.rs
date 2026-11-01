use std::{collections::BTreeMap, future::Future, pin::Pin, sync::Arc};

use async_trait::async_trait;
use blehub::BleBackend;
use bleng::Bleng;

use super::types::*;
use crate::recovery::RecoveryContext;
use crate::{
    PlatformActionResult, PlatformOwnerSession, PlatformOwnerSessionFactory,
    PlatformRecoveryAction, ResourceSnapshot, SupervisorError,
};
/// Foreign implementation of one native platform session.
///
/// `execute_recovery_action` receives only platform-owned actions. Owner
/// retirement and fresh-owner assembly stay inside the Rust supervisor.
#[uniffi::export(foreign)]
#[async_trait]
pub trait ForeignPlatformOwnerSession: Send + Sync {
    fn context(&self) -> ForeignOwnerContext;

    fn backend(&self) -> Arc<dyn BleBackend>;

    fn dispose_unassembled(&self) -> Result<(), SupervisorFfiError>;

    fn start_event_reader(&self, owner: Arc<Bleng>) -> Result<(), SupervisorFfiError>;

    async fn wait_event_reader(&self) -> Result<(), SupervisorFfiError>;

    async fn resource_snapshot(&self) -> Result<Vec<ForeignResourceCount>, SupervisorFfiError>;

    async fn execute_recovery_action(
        &self,
        sequence: u64,
        action: ForeignPlatformRecoveryAction,
    ) -> Result<ForeignPlatformActionResult, SupervisorFfiError>;
}

/// Foreign factory that creates native sessions for a requested context.
#[uniffi::export(foreign)]
pub trait ForeignPlatformOwnerSessionFactory: Send + Sync {
    fn create(
        &self,
        context: ForeignOwnerContext,
    ) -> Result<Arc<dyn ForeignPlatformOwnerSession>, SupervisorFfiError>;
}

/// Rust adapter from the foreign factory to the core supervisor contract.
pub struct ForeignPlatformOwnerSessionFactoryAdapter {
    factory: Arc<dyn ForeignPlatformOwnerSessionFactory>,
}

impl ForeignPlatformOwnerSessionFactoryAdapter {
    pub fn new(factory: Arc<dyn ForeignPlatformOwnerSessionFactory>) -> Self {
        Self { factory }
    }
}

impl PlatformOwnerSessionFactory for ForeignPlatformOwnerSessionFactoryAdapter {
    fn create(
        &self,
        context: &RecoveryContext,
    ) -> Result<Box<dyn PlatformOwnerSession>, SupervisorError> {
        let requested = ForeignOwnerContext::from(context);
        let session = self
            .factory
            .create(requested)
            .map_err(SupervisorFfiError::into_supervisor)?;
        let actual = RecoveryContext::try_from(session.context())
            .map_err(SupervisorFfiError::into_supervisor)?;
        if actual != *context {
            return Err(SupervisorError::PlatformContextMismatch);
        }
        Ok(Box::new(ForeignPlatformOwnerSessionAdapter {
            session,
            context: actual,
        }))
    }
}

struct ForeignPlatformOwnerSessionAdapter {
    session: Arc<dyn ForeignPlatformOwnerSession>,
    context: RecoveryContext,
}

impl PlatformOwnerSession for ForeignPlatformOwnerSessionAdapter {
    fn context(&self) -> &RecoveryContext {
        &self.context
    }

    fn backend(&self) -> Arc<dyn BleBackend> {
        self.session.backend()
    }

    fn dispose_unassembled(&self) -> Result<(), SupervisorError> {
        self.session
            .dispose_unassembled()
            .map_err(SupervisorFfiError::into_supervisor)
    }

    fn start_event_reader(&self, owner: Arc<Bleng>) -> Result<(), SupervisorError> {
        self.session
            .start_event_reader(owner)
            .map_err(SupervisorFfiError::into_supervisor)
    }

    fn wait_event_reader(
        &self,
    ) -> Pin<Box<dyn Future<Output = Result<(), SupervisorError>> + Send + '_>> {
        let session = Arc::clone(&self.session);
        Box::pin(async move {
            session
                .wait_event_reader()
                .await
                .map_err(SupervisorFfiError::into_supervisor)
        })
    }

    fn resource_snapshot(
        &self,
    ) -> Pin<Box<dyn Future<Output = Result<ResourceSnapshot, SupervisorError>> + Send + '_>> {
        let session = Arc::clone(&self.session);
        Box::pin(async move {
            let counts = session
                .resource_snapshot()
                .await
                .map_err(SupervisorFfiError::into_supervisor)?;
            let mut snapshot = BTreeMap::new();
            for resource in counts {
                if snapshot
                    .insert(resource.name.clone(), resource.count)
                    .is_some()
                {
                    return Err(SupervisorError::Platform {
                        detail: format!("duplicate resource name: {}", resource.name),
                    });
                }
            }
            Ok(snapshot)
        })
    }

    fn execute_recovery_action(
        &self,
        sequence: u64,
        action: PlatformRecoveryAction,
    ) -> Pin<Box<dyn Future<Output = Result<PlatformActionResult, SupervisorError>> + Send + '_>>
    {
        let session = Arc::clone(&self.session);
        Box::pin(async move {
            let result = session
                .execute_recovery_action(sequence, action.into())
                .await
                .map_err(SupervisorFfiError::into_supervisor)?;
            Ok(PlatformActionResult {
                sequence: result.sequence,
                signal: result
                    .signal
                    .try_into()
                    .map_err(SupervisorFfiError::into_supervisor)?,
            })
        })
    }
}
