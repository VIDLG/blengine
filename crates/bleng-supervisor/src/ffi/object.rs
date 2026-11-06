use std::{
    collections::BTreeSet,
    sync::{Arc, Mutex, TryLockError},
};

use super::adapter::{
    ForeignPlatformOwnerSessionFactory, ForeignPlatformOwnerSessionFactoryAdapter,
};
use super::types::*;
use super::{ForeignOwnerClose, ForeignOwnerCloseFailure};
use crate::SupervisorConfig;
use crate::recovery::{RecoveryContext, RecoveryLink, RecoveryObservation, RecoveryOperationId};
#[path = "operations.rs"]
mod operations;

#[cfg(test)]
#[path = "tests/close.rs"]
mod close_tests;

/// Native lifecycle and context-fenced operation facade for one Rust recovery supervisor.
#[derive(uniffi::Object)]
pub struct Supervisor {
    inner: Mutex<SupervisorState>,
    close_view: Mutex<Option<ForeignOwnerClose>>,
}

// Busy always has a live InFlight owner. Interrupted never admits more work,
// even if cancellation happened before the underlying lifecycle changed phase.
enum SupervisorState {
    Available(Box<crate::SessionSupervisor>),
    Busy,
    Interrupted,
}

impl SupervisorState {
    fn as_ref(&self) -> Option<&crate::SessionSupervisor> {
        match self {
            Self::Available(supervisor) => Some(supervisor),
            Self::Busy | Self::Interrupted => None,
        }
    }

    fn as_mut(&mut self) -> Option<&mut crate::SessionSupervisor> {
        match self {
            Self::Available(supervisor) => Some(supervisor),
            Self::Busy | Self::Interrupted => None,
        }
    }

    fn take(&mut self) -> Result<Box<crate::SessionSupervisor>, SupervisorFfiError> {
        match self {
            Self::Busy => {
                return Err(SupervisorFfiError::Platform {
                    detail: "supervisor operation is already in progress".to_owned(),
                });
            }
            Self::Interrupted => return Err(Self::unavailable()),
            Self::Available(_) => {}
        }
        let Self::Available(supervisor) = std::mem::replace(self, Self::Busy) else {
            unreachable!("availability checked under the lifecycle lock");
        };
        Ok(supervisor)
    }

    fn unavailable() -> SupervisorFfiError {
        SupervisorFfiError::Platform {
            detail: "supervisor is unavailable after an interrupted lifecycle operation".to_owned(),
        }
    }
}

/// Owns the supervisor outside its mutex across an await. Only explicit restoration
/// commits it back; cancellation/unwind must never revive a partially executed owner.
struct InFlight<'a> {
    facade: &'a Supervisor,
    supervisor: Option<Box<crate::SessionSupervisor>>,
}

impl std::ops::Deref for InFlight<'_> {
    type Target = crate::SessionSupervisor;

    fn deref(&self) -> &Self::Target {
        self.supervisor.as_deref().expect("in-flight owner")
    }
}

impl std::ops::DerefMut for InFlight<'_> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        self.supervisor.as_deref_mut().expect("in-flight owner")
    }
}

impl Drop for InFlight<'_> {
    fn drop(&mut self) {
        let Some(supervisor) = self.supervisor.take() else {
            return;
        };
        if supervisor.is_closed() {
            // Logical closure can precede reader/resource retirement. Preserve a
            // terminal failure, never a successful or resource-zero receipt.
            self.facade.record_close_view(
                true,
                Some(&crate::SupervisorError::Platform {
                    detail: "lifecycle operation interrupted; cleanup completion is unverified"
                        .to_owned(),
                }),
            );
        }
        *self
            .facade
            .inner
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = SupervisorState::Interrupted;
        // No foreign destruction under the facade lock, and no async cleanup in Drop.
        drop(supervisor);
    }
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
            inner: Mutex::new(SupervisorState::Available(Box::new(
                crate::SessionSupervisor::new(factory, config)
                    .map_err(SupervisorFfiError::from_supervisor)?,
            ))),
            close_view: Mutex::new(None),
        }))
    }

    pub fn is_active(&self) -> Result<bool, SupervisorFfiError> {
        Ok(self
            .lock_inner()?
            .as_ref()
            .is_some_and(crate::SessionSupervisor::is_active))
    }

    /// Returns the context of the currently assembled native owner.
    ///
    /// The result is absent before start, during owner retirement/fresh-owner assembly, and after
    /// shutdown. Native hosts must use this value when constructing observations instead of
    /// retaining or recreating owner identity locally.
    pub fn active_context(&self) -> Result<Option<ForeignOwnerContext>, SupervisorFfiError> {
        Ok(self
            .lock_inner()?
            .as_ref()
            .and_then(crate::SessionSupervisor::active_context)
            .map(ForeignOwnerContext::from))
    }

    /// Returns startup evidence for the currently assembled owner, if one exists.
    pub fn active_owner_start(&self) -> Result<Option<ForeignOwnerStart>, SupervisorFfiError> {
        Ok(self
            .lock_inner()?
            .as_ref()
            .and_then(crate::SessionSupervisor::active_owner_start)
            .map(ForeignOwnerStart::from))
    }

    pub async fn start(&self) -> Result<ForeignOwnerStart, SupervisorFfiError> {
        let mut supervisor = self.take_inner()?;
        let result = supervisor
            .start()
            .await
            .map(ForeignOwnerStart::from)
            .map_err(SupervisorFfiError::from_supervisor);
        self.restore_inner(supervisor)?;
        result
    }

    /// Returns the last resource-zero snapshot validated during recovery retirement.
    pub fn last_retired_resources(
        &self,
    ) -> Result<Option<Vec<ForeignResourceCount>>, SupervisorFfiError> {
        Ok(self
            .lock_inner()?
            .as_ref()
            .and_then(crate::SessionSupervisor::last_retired_resources)
            .map(|snapshot| {
                snapshot
                    .iter()
                    .map(|(name, count)| ForeignResourceCount {
                        name: name.clone(),
                        count: *count,
                    })
                    .collect()
            }))
    }

    /// Installs the baseline link and old-operation terminal IDs after the initial owner has
    /// assembled them. This must happen before the first `RadioLost` observation and can only be
    /// called once.
    pub fn configure_old_operation_barrier(
        &self,
        old_link: Option<ForeignRecoveryLink>,
        expected_old_operation_ids: Vec<String>,
    ) -> Result<(), SupervisorFfiError> {
        let old_link = old_link
            .map(RecoveryLink::try_from)
            .transpose()
            .map_err(SupervisorFfiError::from_recovery_configuration)?;
        let expected_terminals = expected_old_operation_ids
            .into_iter()
            .map(|id| {
                RecoveryOperationId::new(id)
                    .map_err(SupervisorFfiError::from_recovery_configuration)
            })
            .collect::<Result<BTreeSet<_>, _>>()?;
        let mut inner = self.lock_inner()?;
        let supervisor = inner.as_mut().ok_or_else(|| SupervisorFfiError::Platform {
            detail: "supervisor operation is already in progress".to_owned(),
        })?;
        supervisor
            .configure_old_operation_barrier(old_link, expected_terminals)
            .map_err(SupervisorFfiError::from_supervisor)
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

    /// Drives platform actions until an owner-boundary action or terminal plan is returned.
    pub async fn drive_platform_actions_until_boundary(
        &self,
    ) -> Result<ForeignRecoveryPlan, SupervisorFfiError> {
        let mut supervisor = self.take_inner()?;
        let result = supervisor
            .drive_platform_actions_until_boundary()
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
        let result = supervisor.shutdown().await;
        self.record_close_view(supervisor.is_closed(), result.as_ref().err());
        self.restore_inner(supervisor)?;
        result
            .map(|snapshot| {
                snapshot
                    .into_iter()
                    .map(|(name, count)| ForeignResourceCount { name, count })
                    .collect()
            })
            .map_err(SupervisorFfiError::from_supervisor)
    }

    /// Returns the first terminal close outcome, including failed cleanup, without retrying it.
    /// A busy/unavailable response is not terminal and is never cached.
    pub async fn shutdown_view(&self) -> ForeignOwnerClose {
        if let Some(view) = self.cached_close_view() {
            return view;
        }
        let mut supervisor = {
            let mut inner = match self.inner.try_lock() {
                Ok(inner) => inner,
                Err(TryLockError::WouldBlock) => return ForeignOwnerClose::busy(),
                Err(TryLockError::Poisoned(_)) => return ForeignOwnerClose::unavailable(),
            };
            // A concurrent close may have published its receipt before restoring inner.
            if let Some(view) = self.cached_close_view() {
                return view;
            }
            let supervisor = match &*inner {
                SupervisorState::Busy => return ForeignOwnerClose::busy(),
                SupervisorState::Interrupted => return ForeignOwnerClose::unavailable(),
                SupervisorState::Available(_) => inner.take().expect("available supervisor"),
            };
            InFlight {
                facade: self,
                supervisor: Some(supervisor),
            }
        };
        let result = supervisor.shutdown().await;
        let view = self.record_close_view(supervisor.is_closed(), result.as_ref().err());
        // Restoration cannot undo the observed terminal state or replace its cleanup outcome.
        let _ = self.restore_inner(supervisor);
        view
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

    /// Accepts an external host signal against Rust's current active owner context.
    /// Platform progress signals must arrive through `execute_platform_action` instead.
    pub fn observe_external_signal(
        &self,
        signal: ForeignExternalRecoverySignal,
    ) -> Result<ForeignRecoveryPlan, SupervisorFfiError> {
        let mut inner = self.lock_inner()?;
        let supervisor = inner.as_mut().ok_or_else(|| SupervisorFfiError::Platform {
            detail: "supervisor operation is already in progress".to_owned(),
        })?;
        let context = supervisor
            .active_context()
            .cloned()
            .ok_or(SupervisorFfiError::NoActiveContext)?;
        supervisor
            .observe(RecoveryObservation::new(context, signal.into()))
            .map(ForeignRecoveryPlan::from)
            .map_err(SupervisorFfiError::from_supervisor)
    }

    /// HIL-only raw observation seam for deterministic lifecycle probes.
    /// Production platform adapters must not use this to report action progress.
    pub fn observe_signal(
        &self,
        signal: ForeignRecoverySignal,
    ) -> Result<ForeignRecoveryPlan, SupervisorFfiError> {
        let mut inner = self.lock_inner()?;
        let supervisor = inner.as_mut().ok_or_else(|| SupervisorFfiError::Platform {
            detail: "supervisor operation is already in progress".to_owned(),
        })?;
        let context = supervisor
            .active_context()
            .cloned()
            .ok_or(SupervisorFfiError::NoActiveContext)?;
        let observation = RecoveryObservation::new(context, signal.try_into()?);
        supervisor
            .observe(observation)
            .map(ForeignRecoveryPlan::from)
            .map_err(SupervisorFfiError::from_supervisor)
    }
}

impl Supervisor {
    fn cached_close_view(&self) -> Option<ForeignOwnerClose> {
        self.close_view
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone()
    }

    fn record_close_view(
        &self,
        owner_closed: bool,
        error: Option<&crate::SupervisorError>,
    ) -> ForeignOwnerClose {
        let view = ForeignOwnerClose {
            owner_closed,
            failure: error.map(ForeignOwnerCloseFailure::from_supervisor),
        };
        if !view.owner_closed {
            return view;
        }
        // Only local receipt access happens under this lock; no await or foreign callback.
        // Publish before restoring inner so either shutdown entry point preserves the first result.
        self.close_view
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .get_or_insert(view)
            .clone()
    }

    fn lock_inner(&self) -> Result<std::sync::MutexGuard<'_, SupervisorState>, SupervisorFfiError> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| SupervisorFfiError::Platform {
                detail: "supervisor mutex is poisoned".to_owned(),
            })?;
        if matches!(*inner, SupervisorState::Interrupted) {
            return Err(SupervisorState::unavailable());
        }
        Ok(inner)
    }

    fn take_inner(&self) -> Result<InFlight<'_>, SupervisorFfiError> {
        let supervisor = self.lock_inner()?.take()?;
        Ok(InFlight {
            facade: self,
            supervisor: Some(supervisor),
        })
    }

    fn restore_inner(&self, mut operation: InFlight<'_>) -> Result<(), SupervisorFfiError> {
        let mut inner = self.lock_inner()?;
        *inner = SupervisorState::Available(operation.supervisor.take().expect("in-flight owner"));
        Ok(())
    }
}
