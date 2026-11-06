import BleHubUniFfi

/// Internal event projection used by the bounded Apple recovery reader.
enum RecoveryEventProjection: Equatable {
  case terminal(UInt64)
  case advertisement(AdvertisementObservation)
  case radioState(AdapterState)
}

/// Keeps command-correlated initialization results out of the recovery radio stream.
///
/// The full BLE event remains available to the typed operation reader; this projection only
/// selects evidence owned by the recovery action reader.
func projectRecoveryEvent(_ event: BleEvent) -> RecoveryEventProjection? {
  switch event {
  case let .commandCompleted(commandId, _):
    return .terminal(commandId.value)
  case let .advertisementObserved(advertisement):
    return .advertisement(advertisement)
  case let .adapterSnapshotChanged(commandId, snapshot):
    guard commandId == nil else { return nil }
    return .radioState(snapshot.state)
  default:
    return nil
  }
}
