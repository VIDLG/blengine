use super::*;

#[test]
fn maximum_id_is_issued_once_and_exhaustion_never_wraps_or_mutates_sequence() {
    let sequence = IdGenerator {
        last_issued: (u64::MAX - 1).into(),
    };
    assert_eq!(sequence.allocate(), Ok(u64::MAX));
    for _ in 0..3 {
        assert_eq!(sequence.allocate(), Err(WaiterError::IdExhausted));
        assert_eq!(sequence.last_issued.load(Ordering::Acquire), u64::MAX);
    }
}
