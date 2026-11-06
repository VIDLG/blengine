use super::*;
use kameo::{actor::PreparedActor, mailbox};
use std::{
    future::{Future, poll_fn},
    task::Poll,
    time::Duration,
};

pub(super) struct WaitPause {
    pub(super) after_result: bool,
    pub(super) entered: oneshot::Sender<()>,
    pub(super) release: oneshot::Receiver<()>,
}

impl<M: OperationSpec> ResultSlot<M> {
    pub(crate) fn pause_wait_for_test(
        &self,
        after_result: bool,
    ) -> (oneshot::Receiver<()>, oneshot::Sender<()>) {
        let (entered, entered_rx) = oneshot::channel();
        let (release_tx, release) = oneshot::channel();
        *self.wait_pause.lock().unwrap() = Some(WaitPause {
            after_result,
            entered,
            release,
        });
        (entered_rx, release_tx)
    }

    pub(crate) async fn wait(&self, timeout: Duration) -> Result<M::Output, WaiterError> {
        self.wait_until(Instant::now() + timeout).await
    }

    pub(crate) async fn wait_until(&self, deadline: Instant) -> Result<M::Output, WaiterError> {
        self.claim_wait_until(deadline)?.await
    }
}

#[test]
fn closed_sender_before_typed_wait_returns_transport_fault() {
    let prepared = PreparedActor::<BlengActor>::new(mailbox::bounded(1));
    let actor = prepared.actor_ref().clone();
    let lifecycle = Arc::new(RuntimeLifecycle::new());
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(async {
        let (sender, receiver) = oneshot::channel();
        drop(sender);
        let mut ticket = WaiterTicket::new(1.into(), receiver, actor, lifecycle);
        ticket.preserve_completed_result_on_close = true;
        assert_eq!(
            ticket
                .wait_until(Instant::now() + Duration::from_secs(1))
                .await,
            Err(WaiterError::TransportFault)
        );
    });
}

#[test]
fn actor_ask_failure_distinguishes_close_from_unexpected_loss() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    runtime.block_on(async {
        for closing in [false, true] {
            let prepared = PreparedActor::<BlengActor>::new(mailbox::bounded(1));
            let lifecycle = Arc::new(RuntimeLifecycle::new());
            let control = OperationControl {
                actor: prepared.actor_ref().clone(),
                lifecycle: Arc::clone(&lifecycle),
                submission: None,
            };
            let mut cancel = Box::pin(control.cancel(1.into()));
            assert!(
                poll_fn(|cx| Poll::Ready(cancel.as_mut().poll(cx)))
                    .await
                    .is_pending()
            );
            if closing {
                lifecycle.request_close();
            }
            drop(prepared);
            assert_eq!(
                tokio::time::timeout(Duration::from_secs(2), cancel)
                    .await
                    .unwrap(),
                Err(if closing {
                    WaiterError::Closed
                } else {
                    WaiterError::TransportFault
                })
            );
            assert_eq!(
                lifecycle.phase(),
                if closing {
                    RuntimePhase::Closed
                } else {
                    RuntimePhase::Faulted
                }
            );
        }
    });
}
