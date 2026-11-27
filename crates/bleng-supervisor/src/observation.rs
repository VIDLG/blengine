//! The owner's one revocable observation boundary, with typed lanes.
//!
//! One owner has exactly one event reader. That reader offers every canonical event to this
//! object, which fans it out to the lanes that currently have a consumer: the scan lane, the
//! connection-end lanes, the notification-subscription lanes, the adapter-state lanes and the
//! companion-presence lanes. Lanes are registered *before* the bounded native submission that
//! creates them, so no event can slip in between a submission and its registration.
//!
//! **Two kinds of lane, and only two.**
//!
//! - *Exclusive*: the scan lane, because a product owner runs one scan at a time. A second
//!   submission is refused while the lane is live (`DeliveryBusy`), never silently given the slot.
//! - *Coexisting*: connection ends, notification subscriptions, adapter observations and companion
//!   presence. Several links, several subscriptions and several readers of the adapter's own state
//!   are normal, so these are [`LaneSet`]s and every event reaches every lane; each lane accepts only
//!   what its own identity and its own completion bind.
//!
//! The distinction is not cosmetic. A coexisting lane reached through a single "current" slot hands
//! the newest consumer every event and starves the earlier ones — a real defect that reached a
//! device once already (a second subscription took over the first one's values). [`LaneSet`] exists
//! so that shape cannot be written again: there is no `current` field to aim at.
//!
//! Revocation is a single owner-scoped fact: retiring the owner closes every lane at once, and a
//! lane registered after revocation is refused. Nothing here infers the "current owner" from
//! process state, and no lane can keep a retired owner alive.

use std::sync::{
    Arc, Mutex, MutexGuard, Weak,
    atomic::{AtomicBool, Ordering},
};

use blehub::{BleEvent, BondState};
use bleng::{BondOperation, ConnectOperation, EnableNotificationsOperation, StartScanOperation};

use crate::recovery::RecoveryContext;
use crate::scan::{ScanEventEnvelope, scan_event};
use crate::{
    AdapterDelivery, AdapterSubmissionError, BondHandoff, BondProgressDelivery,
    BondSubmissionError, CompanionPresence, CompanionPresenceDelivery,
    CompanionPresenceSubmissionError, ConnectionHandoff, ConnectionLane, ConnectionSubmissionError,
    NotificationDelivery, NotificationHandoff, NotificationSource, NotificationSubmissionError,
    ScanDelivery, ScanHandoff, ScanSubmissionError, SupervisorOperationError,
};

/// Owner-scoped projection of the owner's existing public event stream.
///
/// Its source context is fixed at construction and never looked up through the active supervisor.
/// The single reader offers observations to whichever bounded lanes are registered here; no second
/// reader or runtime is created, and weak slots cannot keep a discarded lane alive.
#[derive(Debug)]
pub struct SessionObservationProjection {
    source_context: RecoveryContext,
    revoked: AtomicBool,
    scan: Mutex<ScanDeliveries>,
    connections: Mutex<LaneSet<ConnectionLane>>,
    bonds: Mutex<LaneSet<BondProgressDelivery>>,
    notifications: Mutex<LaneSet<NotificationDelivery>>,
    adapter: Mutex<LaneSet<AdapterDelivery>>,
    presence: Mutex<LaneSet<CompanionPresenceDelivery>>,
    #[cfg(test)]
    pub(crate) install_pause: Mutex<Option<ScanInstallPause>>,
    #[cfg(test)]
    pub(crate) install_contention: Mutex<Option<std::sync::mpsc::SyncSender<()>>>,
}

#[cfg(test)]
#[derive(Debug)]
pub(crate) struct ScanInstallPause {
    pub(crate) submitted: std::sync::mpsc::SyncSender<blehub::CommandId>,
    pub(crate) release: std::sync::mpsc::Receiver<()>,
}

/// Owner-local revocation bookkeeping, not event routing by identity. Entries are weak and
/// pruned on each submission, so discarded handles do not accumulate historical tombstones.
///
/// Scan is deliberately *not* a [`LaneSet`]: one owner runs one scan, so `current` is the whole
/// point and a second live scan is refused rather than given the slot (`can_replace`).
#[derive(Debug, Default)]
struct ScanDeliveries {
    current: Weak<ScanDelivery>,
    live: Vec<Weak<ScanDelivery>>,
}

/// One thing an owner-scoped lane set may own: receive a canonical event, and be ended by this
/// owner (retired, or bookkeeping unavailable).
///
/// `offer` takes only the event: identity fencing is the lane's own job, because only the lane knows
/// which connection, session, attribute and subscription it belongs to.
trait OwnerLane: Sized {
    fn offer(&self, event: &BleEvent);
    fn fail_unavailable(&self);
    fn close(&self);
}

/// The lanes of one owner that coexist, e.g. its links and its notification subscriptions.
///
/// Weak entries keep a discarded handle from pinning an owner, and are pruned on each submission and
/// publication. Publication fans one event out to every live lane; a poisoned set fails all of them
/// instead of silently dropping the event.
#[derive(Debug)]
struct LaneSet<L: OwnerLane> {
    live: Vec<Weak<L>>,
}

impl<L: OwnerLane> Default for LaneSet<L> {
    fn default() -> Self {
        Self { live: Vec::new() }
    }
}

impl<L: OwnerLane> LaneSet<L> {
    /// Register a lane that was installed under this set's lock, before its native submission
    /// returned. Pruning here is also owner-local cleanup of lanes nobody holds any more.
    fn install(&mut self, lane: &Arc<L>) {
        self.live.retain(|entry| entry.strong_count() > 0);
        self.live.push(Arc::downgrade(lane));
    }

    fn fan_out(&mut self, event: &BleEvent, poisoned: bool) {
        self.live.retain(|entry| entry.strong_count() > 0);
        if poisoned {
            for lane in self.live.drain(..).filter_map(|entry| entry.upgrade()) {
                lane.fail_unavailable();
            }
            return;
        }
        for lane in self.live.iter().filter_map(|entry| entry.upgrade()) {
            lane.offer(event);
        }
    }

    fn close_all(&mut self) {
        for lane in self.live.drain(..).filter_map(|entry| entry.upgrade()) {
            lane.close();
        }
    }
}

impl OwnerLane for NotificationDelivery {
    fn offer(&self, event: &BleEvent) {
        NotificationDelivery::offer(self, event);
    }

    fn fail_unavailable(&self) {
        NotificationDelivery::fail_unavailable(self);
    }

    fn close(&self) {
        NotificationDelivery::close(self);
    }
}

impl OwnerLane for ConnectionLane {
    fn offer(&self, event: &BleEvent) {
        ConnectionLane::offer(self, event);
    }

    fn fail_unavailable(&self) {
        ConnectionLane::fail_unavailable(self);
    }

    fn close(&self) {
        ConnectionLane::close(self);
    }
}

impl OwnerLane for BondProgressDelivery {
    fn offer(&self, event: &BleEvent) {
        BondProgressDelivery::offer(self, event);
    }

    fn fail_unavailable(&self) {
        BondProgressDelivery::fail_unavailable(self);
    }

    fn close(&self) {
        BondProgressDelivery::close(self);
    }
}

impl OwnerLane for AdapterDelivery {
    fn offer(&self, event: &BleEvent) {
        // The lane owns its own projection: a canonical adapter snapshot is the only thing it
        // stores, and every other event is somebody else's lane.
        if let BleEvent::AdapterSnapshotChanged { snapshot, .. } = event {
            AdapterDelivery::offer(self, snapshot);
        }
    }

    fn fail_unavailable(&self) {
        AdapterDelivery::fail_unavailable(self);
    }

    fn close(&self) {
        AdapterDelivery::close(self);
    }
}

impl OwnerLane for CompanionPresenceDelivery {
    fn offer(&self, event: &BleEvent) {
        // Presence is uncorrelated with any command, so the lane accepts the one event that carries
        // its own fact and ignores every other event.
        if let BleEvent::CompanionPresenceChanged {
            present,
            key,
            local_name,
        } = event
        {
            CompanionPresenceDelivery::offer(
                self,
                CompanionPresence {
                    present: *present,
                    key: key.clone(),
                    local_name: local_name.clone(),
                },
            );
        }
    }

    fn fail_unavailable(&self) {
        CompanionPresenceDelivery::fail_unavailable(self);
    }

    fn close(&self) {
        CompanionPresenceDelivery::close(self);
    }
}

impl SessionObservationProjection {
    pub(crate) fn submit_scan(
        &self,
        submit: impl FnOnce() -> Result<Arc<StartScanOperation>, SupervisorOperationError>,
    ) -> Result<ScanHandoff, ScanSubmissionError> {
        let mut slot = self
            .scan
            .lock()
            .map_err(|_| ScanSubmissionError::Unavailable)?;
        if self.revoked.load(Ordering::Acquire) {
            return Err(ScanSubmissionError::OwnerRetired);
        }
        if let Some(delivery) = slot.current.upgrade()
            && !delivery.can_replace()
        {
            return Err(ScanSubmissionError::DeliveryBusy);
        }
        // Hold only across bounded, synchronous BLeng submission, never BLE/wait/foreign code.
        // A reader callback arriving before return cannot pass this lock until the command-bound
        // slot is installed. Submission failure leaves the earlier slot untouched.
        let operation = submit()?;
        #[cfg(test)]
        if let Some(pause) = self.install_pause.lock().unwrap().take() {
            pause.submitted.send(operation.command_id()).unwrap();
            pause
                .release
                .recv_timeout(std::time::Duration::from_secs(2))
                .expect("scan installation rendezvous was not released");
        }
        let delivery = Arc::new(ScanDelivery::new(
            self.source_context.clone(),
            operation.command_id(),
        ));
        delivery.observe_failure(&operation);
        slot.live.retain(|entry| entry.strong_count() > 0);
        slot.current = Arc::downgrade(&delivery);
        slot.live.push(Arc::downgrade(&delivery));
        Ok(ScanHandoff {
            operation,
            delivery,
        })
    }

    /// Registers a notification lane with the same atomicity as scan: the lane exists before the
    /// enable is submitted, so the enable's own state change is what binds it.
    ///
    /// Subscriptions coexist, exactly like links: one owner may hold a subscription per link or per
    /// characteristic, and each lane accepts only its own source and its own enable completion.
    /// A single "current" slot here would make a later subscription steal an earlier one's values.
    pub(crate) fn submit_notifications(
        &self,
        source: NotificationSource,
        submit: impl FnOnce() -> Result<Arc<EnableNotificationsOperation>, SupervisorOperationError>,
    ) -> Result<NotificationHandoff, NotificationSubmissionError> {
        let mut slot = self
            .notifications
            .lock()
            .map_err(|_| NotificationSubmissionError::Unavailable)?;
        if self.revoked.load(Ordering::Acquire) {
            return Err(NotificationSubmissionError::OwnerRetired);
        }
        // Hold only across bounded, synchronous BLeng submission, never BLE/wait/foreign code.
        // A reader callback arriving before return cannot pass this lock until the command-bound
        // lane is installed.
        let operation = submit()?;
        let delivery = Arc::new(NotificationDelivery::new(
            self.source_context.clone(),
            operation.command_id(),
            source,
        ));
        slot.install(&delivery);
        Ok(NotificationHandoff {
            operation,
            delivery,
        })
    }

    /// Registers an adapter-state lane. There is no native submission to interleave with: the
    /// adapter is already initialized, and every later snapshot change on the owner's FIFO is what
    /// the lane will see. Registration and revocation share the same lock, so a lane is either
    /// closed by retirement or sees only the events that follow its own registration.
    pub(crate) fn observe_adapter(&self) -> Result<Arc<AdapterDelivery>, AdapterSubmissionError> {
        let mut slot = self
            .adapter
            .lock()
            .map_err(|_| AdapterSubmissionError::Unavailable)?;
        if self.revoked.load(Ordering::Acquire) {
            return Err(AdapterSubmissionError::OwnerRetired);
        }
        let delivery = Arc::new(AdapterDelivery::new());
        slot.install(&delivery);
        Ok(delivery)
    }

    /// Registers a companion-presence lane. Like the adapter lane there is no native submission to
    /// interleave with: the association already exists and every later edge-triggered transition on
    /// the owner's FIFO is what the lane will see. Registration and revocation share the same lock,
    /// so a lane is either closed by retirement or sees only the transitions after it was installed.
    pub(crate) fn observe_companion_presence(
        &self,
    ) -> Result<Arc<CompanionPresenceDelivery>, CompanionPresenceSubmissionError> {
        let mut slot = self
            .presence
            .lock()
            .map_err(|_| CompanionPresenceSubmissionError::Unavailable)?;
        if self.revoked.load(Ordering::Acquire) {
            return Err(CompanionPresenceSubmissionError::OwnerRetired);
        }
        let delivery = Arc::new(CompanionPresenceDelivery::new());
        slot.install(&delivery);
        Ok(delivery)
    }

    /// Registers a connection-end lane before the connect is submitted, so the connect's own state
    /// change binds it and no end can be missed in between. Links coexist, so this never replaces an
    /// earlier lane: it installs into the owner's connection lane set.
    pub(crate) fn submit_connect(
        &self,
        submit: impl FnOnce() -> Result<Arc<ConnectOperation>, SupervisorOperationError>,
    ) -> Result<ConnectionHandoff, ConnectionSubmissionError> {
        let mut slots = self
            .connections
            .lock()
            .map_err(|_| ConnectionSubmissionError::Unavailable)?;
        if self.revoked.load(Ordering::Acquire) {
            return Err(ConnectionSubmissionError::OwnerRetired);
        }
        // Hold only across bounded, synchronous BLeng submission, never BLE/wait/foreign code.
        let operation = submit()?;
        let lane = Arc::new(ConnectionLane::new(
            self.source_context.clone(),
            operation.command_id(),
        ));
        slots.install(&lane);
        Ok(ConnectionHandoff { operation, lane })
    }

    /// Installs the bond-progress lane atomically with the original bond submission, so the
    /// first platform-reported state cannot slip in between.
    pub(crate) fn submit_bond(
        &self,
        connection: blehub::ConnectionId,
        success_state: BondState,
        submit: impl FnOnce() -> Result<Arc<BondOperation>, SupervisorOperationError>,
    ) -> Result<BondHandoff, BondSubmissionError> {
        let mut slots = self
            .bonds
            .lock()
            .map_err(|_| BondSubmissionError::Unavailable)?;
        if self.revoked.load(Ordering::Acquire) {
            return Err(BondSubmissionError::OwnerRetired);
        }
        // Hold only across bounded, synchronous BLeng submission, never BLE/wait/foreign code.
        let operation = submit()?;
        let lane = Arc::new(BondProgressDelivery::new(
            self.source_context.clone(),
            operation.command_id(),
            connection,
            success_state,
        ));
        slots.install(&lane);
        Ok(BondHandoff { operation, lane })
    }

    /// Fans one canonical event out to the lanes that own it, and returns the scan projection.
    ///
    /// Publication linearizes at the monotonic revocation flag's atomic read. A projection whose
    /// read precedes revocation may still return or be delivered afterward; retirement does not
    /// retract already returned envelopes. Downstream must fence delivery by the full context.
    /// Revocation is not evidence of native scan stop, subscription teardown or resource cleanup.
    pub fn project(&self, event: BleEvent) -> Option<ScanEventEnvelope> {
        if self.revoked.load(Ordering::Acquire) {
            return None;
        }
        let projected = scan_event(event.clone());
        {
            #[cfg(test)]
            if let Some(contended) = self.install_contention.lock().unwrap().take() {
                assert!(matches!(
                    self.scan.try_lock(),
                    Err(std::sync::TryLockError::WouldBlock)
                ));
                contended.send(()).unwrap();
            }
            if self.revoked.load(Ordering::Acquire) {
                return None;
            }
            if let Some(projected) = &projected {
                let slot = self.lock_scan();
                if let Some(delivery) = slot.current.upgrade() {
                    if self.scan.is_poisoned() {
                        delivery.fail_unavailable();
                    } else {
                        delivery.offer(&self.source_context, projected);
                    }
                }
            }
            // A connection end is the only connection fact a lane consumes, so the hot path (every
            // advertisement) never touches the connection lanes at all.
            if matches!(event, BleEvent::ConnectionStateChanged { .. }) {
                let mut slot = self.lock_connections();
                slot.fan_out(&event, self.connections.is_poisoned());
            }
            // Bond progress is correlated by command id, so it is offered only the events it
            // could own; the hot advertisement path never touches these lanes.
            if matches!(
                event,
                BleEvent::BondStateChanged { .. }
                    | BleEvent::CommandCompleted { .. }
                    | BleEvent::ConnectionStateChanged { .. }
            ) {
                let mut slot = self.lock_bond();
                slot.fan_out(&event, self.bonds.is_poisoned());
            }
            // Likewise adapter state: an advertisement stream never touches these lanes.
            if matches!(event, BleEvent::AdapterSnapshotChanged { .. }) {
                let mut slot = self.lock_adapter();
                slot.fan_out(&event, self.adapter.is_poisoned());
            }
            // Companion presence is edge-triggered and uncorrelated with any command, so it reaches
            // these lanes exactly like the adapter's own stream does.
            if matches!(event, BleEvent::CompanionPresenceChanged { .. }) {
                let mut slot = self.lock_presence();
                slot.fan_out(&event, self.presence.is_poisoned());
            }
            // Subscription lanes consume state, values and link loss only. Gating here keeps an
            // advertisement stream from taking one lock per live subscription for nothing.
            if matches!(
                event,
                BleEvent::NotificationStateChanged { .. }
                    | BleEvent::NotificationReceived { .. }
                    | BleEvent::ConnectionStateChanged { .. }
            ) {
                let mut slot = self.lock_notification();
                slot.fan_out(&event, self.notifications.is_poisoned());
            }
        }
        projected.map(|event| ScanEventEnvelope::new(self.source_context.clone(), event))
    }

    fn lock_scan(&self) -> MutexGuard<'_, ScanDeliveries> {
        self.scan
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn lock_connections(&self) -> MutexGuard<'_, LaneSet<ConnectionLane>> {
        self.connections
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn lock_bond(&self) -> MutexGuard<'_, LaneSet<BondProgressDelivery>> {
        self.bonds
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn lock_notification(&self) -> MutexGuard<'_, LaneSet<NotificationDelivery>> {
        self.notifications
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn lock_adapter(&self) -> MutexGuard<'_, LaneSet<AdapterDelivery>> {
        self.adapter
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn lock_presence(&self) -> MutexGuard<'_, LaneSet<CompanionPresenceDelivery>> {
        self.presence
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

/// The non-clone owner capability; retained lane endpoints cannot keep publication alive.
/// Dropping a canceled startup future or active owner revokes even without async cleanup.
pub(crate) struct ObservationProjectionLease {
    projection: Arc<SessionObservationProjection>,
}

impl ObservationProjectionLease {
    pub(crate) fn new(source_context: RecoveryContext) -> Self {
        Self {
            projection: Arc::new(SessionObservationProjection {
                source_context,
                revoked: AtomicBool::new(false),
                scan: Mutex::new(ScanDeliveries::default()),
                connections: Mutex::new(LaneSet::default()),
                bonds: Mutex::new(LaneSet::default()),
                notifications: Mutex::new(LaneSet::default()),
                adapter: Mutex::new(LaneSet::default()),
                presence: Mutex::new(LaneSet::default()),
                #[cfg(test)]
                install_pause: Mutex::new(None),
                #[cfg(test)]
                install_contention: Mutex::new(None),
            }),
        }
    }

    pub(crate) fn projection(&self) -> Arc<SessionObservationProjection> {
        Arc::clone(&self.projection)
    }

    /// Closes every lane while holding all lane locks, so a concurrent submission either sees the
    /// revocation flag or finishes before the drain. Locks are always taken in this order.
    pub(crate) fn revoke(&self) {
        let mut scan = self.projection.lock_scan();
        let mut connections = self.projection.lock_connections();
        let mut bonds = self.projection.lock_bond();
        let mut notifications = self.projection.lock_notification();
        let mut adapter = self.projection.lock_adapter();
        let mut presence = self.projection.lock_presence();
        self.projection.revoked.store(true, Ordering::Release);
        for delivery in scan.live.drain(..).filter_map(|entry| entry.upgrade()) {
            delivery.close();
        }
        scan.current = Weak::new();
        connections.close_all();
        bonds.close_all();
        notifications.close_all();
        adapter.close_all();
        presence.close_all();
    }
}

impl Drop for ObservationProjectionLease {
    fn drop(&mut self) {
        self.revoke();
    }
}

#[cfg(test)]
#[path = "tests/observation_lanes.rs"]
mod tests;
