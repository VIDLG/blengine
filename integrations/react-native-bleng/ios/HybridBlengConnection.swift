import BleHubUniFfi
import NitroModules

/// Mechanical projection of Rust's connection DTOs. No BLE outcome is inferred here.
enum BlengConnectionMapping {
  static func bridgeFailure() -> BlengFailureInfo {
    BlengFailureMapping.bridgeFailure(
      domain: .delivery, message: "Native connection bridge failed."
    )
  }

  static func priority(_ value: BlengConnectionPriority) -> ConnectionPriority {
    switch value {
    case .balanced: return .balanced
    case .high: return .high
    case .lowpower: return .lowPower
    }
  }

  static func prioritySubmission(
    _ value: ForeignPriorityBeginResult
  ) -> BlengPrioritySubmission {
    switch (value.priority, value.failure) {
    case let (.some(priority), nil):
      return .first(BlengPriorityAccepted(priority: HybridBlengPriority(priority: priority)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func bondState(_ value: BondState) -> BlengBondState {
    switch value {
    case BondState.none: return BlengBondState.none
    case BondState.bonding: return BlengBondState.bonding
    case BondState.bonded: return BlengBondState.bonded
    }
  }

  static func bondSubmission(_ value: ForeignBondBeginResult) -> BlengBondSubmission {
    switch (value.bond, value.failure) {
    case let (.some(bond), nil):
      return .first(BlengBondAccepted(bond: HybridBlengBond(bond: bond)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func bondResult(_ value: ForeignBondValueResult) -> BlengBondResult {
    switch (value.state, value.failure) {
    case let (.some(state), nil):
      return .first(BlengBondOutcome(state: bondState(state)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func bondProgress(_ value: ForeignBondProgress) -> BlengBondProgressResult {
    switch (value.state, value.failure) {
    case let (.some(state), nil):
      return .first(BlengBondProgress(state: bondState(state), ended: value.ended))
    case (nil, nil):
      // The retained end of the lane: no further states, and the terminal receipt is `wait`'s.
      return .first(BlengBondProgress(state: nil, ended: value.ended))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func refreshSubmission(_ value: ForeignRefreshBeginResult) -> BlengGattCacheRefreshSubmission {
    switch (value.refresh, value.failure) {
    case let (.some(refresh), nil):
      return .first(BlengGattCacheRefreshAccepted(refresh: HybridBlengGattCacheRefresh(refresh: refresh)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func connectSubmission(
    _ value: ForeignConnectBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengConnectSubmission {
    switch (value.connect, value.failure) {
    case let (.some(connect), nil):
      return .first(
        BlengConnectAccepted(connect: HybridBlengConnect(connect: connect, engineAdmission: engineAdmission))
      )
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func connectResult(
    _ value: ForeignConnectResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengConnectResult {
    switch (value.connection, value.failure) {
    case let (.some(connection), nil):
      return .first(
        BlengConnectOutcome(
          connection: HybridBlengConnection(connection: connection, engineAdmission: engineAdmission)
        )
      )
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  static func disconnectSubmission(
    _ value: ForeignDisconnectBeginResult,
    engineAdmission: AppleEngineAdmission? = nil
  ) -> BlengDisconnectSubmission {
    switch (value.disconnect, value.failure) {
    case let (.some(disconnect), nil):
      return .first(BlengDisconnectAccepted(disconnect: HybridBlengDisconnect(disconnect: disconnect)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  /// One end observation: this connection's retained terminal, or why it could not be observed.
  static func endObservation(_ value: ForeignConnectionEndObservation) -> BlengConnectionNext {
    switch (value.ended, value.failure) {
    case let (.some(ended), nil):
      return .first(BlengConnectionEndedEvent(ended: end(ended)))
    case let (nil, .some(error)):
      return .second(BlengFailure(failure: BlengFailureMapping.failure(error)))
    default:
      return .second(BlengFailure(failure: bridgeFailure()))
    }
  }

  private static func end(_ value: ForeignConnectionEnd) -> BlengEnd {
    let reason: BlengEndReason
    switch value.by {
    case .operator: reason = .requested
    case .unrequested: reason = .unrequested
    case .owner: reason = .ownerclosed
    }
    // An absent platform report stays absent; it is never turned into "clean".
    return BlengEnd(
      reason: reason,
      failure: value.failure.map { BlengFailureMapping.failure($0) }
    )
  }
}

/// Owns only the Rust connect proxy, never the root owner.
final class HybridBlengConnect: HybridBlengConnectSpec {
  private let connect: SupervisedConnect
  private let engineAdmission: AppleEngineAdmission?

  init(connect: SupervisedConnect, engineAdmission: AppleEngineAdmission? = nil) {
    self.connect = connect
    self.engineAdmission = engineAdmission
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengConnectResult> {
    let connect = self.connect
    let engineAdmission = self.engineAdmission
    return blengPromise {
      BlengConnectionMapping.connectResult(
        await connect.waitResult(timeoutMs: timeoutMs), engineAdmission: engineAdmission
      )
    }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let connect = self.connect
    return blengPromise { BlengFailureMapping.command(await connect.cancelResult()) }
  }
}

/// Owner-scoped capability; the exact Rust proxy decides which owner may disconnect it.
final class HybridBlengConnection: HybridBlengConnectionSpec {
  private let connection: SupervisedConnection
  private let engineAdmission: AppleEngineAdmission?

  init(connection: SupervisedConnection, engineAdmission: AppleEngineAdmission? = nil) {
    self.connection = connection
    self.engineAdmission = engineAdmission
    super.init()
  }

  func beginDisconnect() throws -> BlengDisconnectSubmission {
    guard let engineAdmission else {
      return BlengConnectionMapping.disconnectSubmission(connection.beginDisconnect())
    }
    return engineAdmission.submit {
      BlengConnectionMapping.disconnectSubmission(connection.beginDisconnect())
    } ?? .second(BlengFailure(failure: BlengConnectionMapping.bridgeFailure()))
  }

  func beginDiscoverGatt() throws -> BlengGattDiscoverySubmission {
    guard let engineAdmission else {
      return BlengGattMapping.discoverySubmission(connection.beginDiscoverGatt())
    }
    return engineAdmission.submit {
      BlengGattMapping.discoverySubmission(connection.beginDiscoverGatt())
    } ?? .second(BlengFailure(failure: BlengGattMapping.bridgeFailure()))
  }

  func beginReadRssi() throws -> BlengRssiSubmission {
    guard let engineAdmission else {
      return BlengGattMapping.rssiSubmission(connection.beginReadRssi())
    }
    return engineAdmission.submit {
      BlengGattMapping.rssiSubmission(connection.beginReadRssi())
    } ?? .second(BlengFailure(failure: BlengGattMapping.bridgeFailure()))
  }

  func beginRequestMtu(mtu: Double) throws -> BlengMtuSubmission {
    guard mtu.isFinite, mtu.rounded() == mtu, (23...517).contains(mtu) else {
      return .second(
        BlengFailure(failure: BlengGattMapping.invalidMtu("mtu must be an integer between 23 and 517"))
      )
    }
    let value = UInt16(mtu)
    guard let engineAdmission else {
      return BlengGattMapping.mtuSubmission(connection.beginRequestMtu(mtu: value))
    }
    return engineAdmission.submit {
      BlengGattMapping.mtuSubmission(connection.beginRequestMtu(mtu: value))
    } ?? .second(BlengFailure(failure: BlengGattMapping.bridgeFailure()))
  }

  func beginQueryMtu() throws -> BlengMtuSubmission {
    guard let engineAdmission else {
      return BlengGattMapping.mtuSubmission(connection.beginQueryMtu())
    }
    return engineAdmission.submit {
      BlengGattMapping.mtuSubmission(connection.beginQueryMtu())
    } ?? .second(BlengFailure(failure: BlengGattMapping.bridgeFailure()))
  }

  func beginSetConnectionPriority(priority: BlengConnectionPriority) throws
    -> BlengPrioritySubmission
  {
    let mapped = BlengConnectionMapping.priority(priority)
    guard let engineAdmission else {
      return BlengConnectionMapping.prioritySubmission(
        connection.beginSetConnectionPriority(priority: mapped))
    }
    return engineAdmission.submit {
      BlengConnectionMapping.prioritySubmission(
        connection.beginSetConnectionPriority(priority: mapped))
    } ?? .second(BlengFailure(failure: BlengConnectionMapping.bridgeFailure()))
  }

  func beginBond(pin: String?) throws -> BlengBondSubmission {
    guard let engineAdmission else {
      return BlengConnectionMapping.bondSubmission(connection.beginCreateBond(pin: pin))
    }
    return engineAdmission.submit {
      BlengConnectionMapping.bondSubmission(connection.beginCreateBond(pin: pin))
    } ?? .second(BlengFailure(failure: BlengConnectionMapping.bridgeFailure()))
  }

  func beginRemoveBond() throws -> BlengBondSubmission {
    guard let engineAdmission else {
      return BlengConnectionMapping.bondSubmission(connection.beginRemoveBond())
    }
    return engineAdmission.submit {
      BlengConnectionMapping.bondSubmission(connection.beginRemoveBond())
    } ?? .second(BlengFailure(failure: BlengConnectionMapping.bridgeFailure()))
  }

  func isConnected() -> Bool {
    connection.isConnected()
  }

  func localName() -> String? {
    connection.localName()
  }

  func beginGattCacheRefresh() throws -> BlengGattCacheRefreshSubmission {
    guard let engineAdmission else {
      return BlengConnectionMapping.refreshSubmission(connection.beginRefreshGattCache())
    }
    return engineAdmission.submit {
      BlengConnectionMapping.refreshSubmission(connection.beginRefreshGattCache())
    } ?? .second(BlengFailure(failure: BlengConnectionMapping.bridgeFailure()))
  }

  func awaitEnd(timeoutMs: Double) throws -> Promise<BlengConnectionNext> {
    let connection = self.connection
    return blengPromise {
      BlengConnectionMapping.endObservation(await connection.awaitEnd(timeoutMs: timeoutMs))
    }
  }
}

final class HybridBlengPriority: HybridBlengPrioritySpec {
  private let priority: SupervisedConnectionPriority

  init(priority: SupervisedConnectionPriority) {
    self.priority = priority
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let priority = self.priority
    return blengPromise {
      BlengFailureMapping.command(await priority.waitResult(timeoutMs: timeoutMs))
    }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let priority = self.priority
    return blengPromise { BlengFailureMapping.command(await priority.cancelResult()) }
  }
}

final class HybridBlengBond: HybridBlengBondSpec {
  private let bond: SupervisedBond

  init(bond: SupervisedBond) {
    self.bond = bond
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengBondResult> {
    let bond = self.bond
    return blengPromise {
      BlengConnectionMapping.bondResult(await bond.waitResult(timeoutMs: timeoutMs))
    }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let bond = self.bond
    return blengPromise { BlengFailureMapping.command(await bond.cancelResult()) }
  }

  func nextState(timeoutMs: Double) throws -> Promise<BlengBondProgressResult> {
    let bond = self.bond
    return blengPromise {
      BlengConnectionMapping.bondProgress(await bond.nextState(timeoutMs: timeoutMs))
    }
  }
}

final class HybridBlengDisconnect: HybridBlengDisconnectSpec {
  private let disconnect: SupervisedDisconnect

  init(disconnect: SupervisedDisconnect) {
    self.disconnect = disconnect
    super.init()
  }

  func wait(timeoutMs: Double) throws -> Promise<BlengReceipt> {
    let disconnect = self.disconnect
    return blengPromise { BlengFailureMapping.command(await disconnect.waitResult(timeoutMs: timeoutMs)) }
  }

  func cancel() throws -> Promise<BlengReceipt> {
    let disconnect = self.disconnect
    return blengPromise { BlengFailureMapping.command(await disconnect.cancelResult()) }
  }
}

/// Per-advertisement capability. Admission goes through the engine lifetime that produced this
/// observation, so a torn-down engine cannot start new native work.
final class HybridBlengPeripheral: HybridBlengPeripheralSpec {
  private let peripheral: SupervisedPeripheral
  private let engineAdmission: AppleEngineAdmission?

  init(peripheral: SupervisedPeripheral, engineAdmission: AppleEngineAdmission? = nil) {
    self.peripheral = peripheral
    self.engineAdmission = engineAdmission
    super.init()
  }

  func key() -> String {
    peripheral.key()
  }

  func localName() -> String? {
    peripheral.localName()
  }

  func beginConnect() throws -> BlengConnectSubmission {
    guard let engineAdmission else {
      return BlengConnectionMapping.connectSubmission(peripheral.beginConnect(autoConnect: false))
    }
    return engineAdmission.submit {
      BlengConnectionMapping.connectSubmission(peripheral.beginConnect(autoConnect: false))
    } ?? .second(BlengFailure(failure: BlengConnectionMapping.bridgeFailure()))
  }

  func beginAutoConnect() throws -> BlengConnectSubmission {
    guard let engineAdmission else {
      return BlengConnectionMapping.connectSubmission(peripheral.beginConnect(autoConnect: true))
    }
    return engineAdmission.submit {
      BlengConnectionMapping.connectSubmission(peripheral.beginConnect(autoConnect: true))
    } ?? .second(BlengFailure(failure: BlengConnectionMapping.bridgeFailure()))
  }
}
