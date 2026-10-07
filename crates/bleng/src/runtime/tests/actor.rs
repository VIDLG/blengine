use super::{BlengActor, IdGenerator};
use crate::{runtime::RuntimeLifecycle, waiter::WaiterError};
use blehub::BleEvent;
use std::sync::Arc;

#[test]
fn maximum_id_is_issued_once_and_exhaustion_never_wraps_or_mutates_sequence() {
    let mut sequence = IdGenerator {
        last_issued: u64::MAX - 1,
    };
    assert_eq!(sequence.allocate(), Ok(u64::MAX));
    for _ in 0..3 {
        assert_eq!(sequence.allocate(), Err(WaiterError::IdExhausted));
        assert_eq!(sequence.last_issued, u64::MAX);
    }
}

#[test]
fn event_publication_rejects_a_closed_or_faulted_gate_before_enqueueing() {
    use tokio::sync::mpsc::{channel, error::TryRecvError};

    for faulted in [false, true] {
        let lifecycle = Arc::new(RuntimeLifecycle::new());
        let (event_tx, mut event_rx) = channel(1);
        let actor = BlengActor::new(event_tx, Arc::clone(&lifecycle));
        if faulted {
            lifecycle.fault();
        } else {
            lifecycle.close();
        }
        assert_eq!(
            actor.publish_event(&BleEvent::CommandCompleted {
                command_id: 1.into(),
                error: None,
            }),
            Err(if faulted {
                WaiterError::TransportFault
            } else {
                WaiterError::Closed
            })
        );
        assert_eq!(event_rx.try_recv(), Err(TryRecvError::Empty));
    }
}
