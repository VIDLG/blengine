//! Cross-reservation invariants exercised through the collection API.
//! Subscription-local tests are mounted by subscription.rs in its own privacy boundary.

use super::{ConnectionReservations, InitializationState, ReservationCoordinator, ScanState};

impl ReservationCoordinator {
    pub(crate) fn test_initialization(&mut self) -> &mut InitializationState {
        &mut self.initialization
    }

    pub(crate) fn test_scan(&mut self) -> &mut ScanState {
        &mut self.scan
    }

    pub(crate) fn test_connections(&mut self) -> &mut ConnectionReservations {
        &mut self.connections
    }
}

mod connections;
mod dispatch;
