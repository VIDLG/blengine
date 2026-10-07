use super::support::{
    Arc, BleHub, Duration, EventIngressStatus, FakeBackend, RuntimeOwner, event, event_with_message,
};

#[test]
fn concurrent_event_readers_consume_one_shared_fifo_without_duplication() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, backend_handle) = FakeBackend::scripted([]);
    let hub = BleHub::new(Arc::new(backend), owner.event_sink()).unwrap();
    owner.attach_hub(hub).unwrap();
    let first = event_with_message("first");
    let second = event_with_message("second");
    assert_eq!(
        backend_handle.emit(first.clone()),
        EventIngressStatus::Delivered
    );
    assert_eq!(
        backend_handle.emit(second.clone()),
        EventIngressStatus::Delivered
    );

    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        let (first_result, second_result) = tokio::join!(
            owner.next_event(Duration::from_secs(2)),
            owner.next_event(Duration::from_secs(2))
        );
        let received = [
            first_result.unwrap().unwrap(),
            second_result.unwrap().unwrap(),
        ];
        assert!(received.contains(&first));
        assert!(received.contains(&second));
        owner.close().await.unwrap();
    });
    assert_eq!(backend_handle.dispose_count(), 1);
}

#[test]
fn real_hub_ingress_reaches_bounded_actor_transport_and_closes() {
    let owner = RuntimeOwner::new().unwrap();
    let (backend, backend_handle) = FakeBackend::scripted([]);
    let sink = owner.event_sink();
    let hub = BleHub::new(Arc::new(backend), Arc::clone(&sink)).unwrap();
    let late = backend_handle.ingress_for_callback().unwrap();
    owner.attach_hub(hub).unwrap();
    assert_eq!(backend_handle.emit(event()), EventIngressStatus::Delivered);
    let host = tokio::runtime::Builder::new_multi_thread()
        .enable_time()
        .build()
        .unwrap();
    host.block_on(async {
        assert_eq!(
            owner.next_event(Duration::from_secs(2)).await,
            Ok(Some(event()))
        );
        owner.close().await.unwrap();
        assert_eq!(owner.next_event(Duration::from_secs(2)).await, Ok(None));
    });
    assert!(sink.on_event(event()).is_err());
    assert!(
        !owner.faulted_for_test(),
        "normal close must not latch transport fault"
    );
    assert_eq!(late.push(event()), EventIngressStatus::Closed);
    assert_eq!(backend_handle.dispose_count(), 1);
}
