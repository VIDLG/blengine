import BleHubUniFfi
import NitroModules
import XCTest
@testable import BleHubReactNativeBleng

/// One multi-link isolation scene on a real device, driven from inside the App process.
///
/// The host owns the phone lease, one leased fixture per declared peer and the evidence bundle;
/// this test owns only the product surface. Stage markers go through `NSLog` so the host can
/// advance its own fixtures, and they are diagnostics rather than a result: every claim below is
/// an assertion on the public Nitro surface. The stage vocabulary is shared with the Android lane
/// (`tools/react_native/bleng_multi_link_contract.py`), so both platforms drive one contract.
///
/// The peer list arrives in the run environment (`BLEHUB_MULTI_PEERS`), so the topology is the
/// declaration and this file holds no device name, port, UUID or profile.
final class BlengMultiLinkDeviceTests: XCTestCase {
  private static let markerTag = "BLEHUB_RN_MULTI"
  // Diagnostics only: absorbs the syslog relay's startup window, which drops lines a
  // just-launched process writes. Never the marker tag, so a probe can never become a stage.
  private static let probeTag = "BLEHUB_RN_MULTI_SYNC"
  private static let probeCount = 8
  private static let probeGapNanoseconds: UInt64 = 250_000_000
  private static let operationDeadlineMs: Double = 10_000
  private static let valueDeadlineMs: Double = 20_000
  private static let endDeadlineMs: Double = 30_000
  private static let settleSeconds: TimeInterval = 60

  private struct Peer {
    let serviceUuid: String
    let localName: String
    let readWriteUuid: String
    let updatesUuid: String
    let valueHex: String
    let writeHex: String
    let mode: BlengNotificationMode
  }

  private struct SceneFailure: Error, CustomStringConvertible {
    let description: String
  }

  private var central: HybridBlengCentral!
  private var lifetime: BlengEngineLifetime?
  private var peers: [Peer] = []
  private var faultPeer: Int?

  override func setUp() async throws {
    let environment = ProcessInfo.processInfo.environment
    guard environment["BLEHUB_MULTI_LINK"] == "multi-link" else {
      throw XCTSkip("requires the leased multi-fixture host")
    }
    peers = try Self.declaredPeers(environment)
    faultPeer = try Self.declaredFault(environment, peers: peers)
  }

  override func tearDown() async throws {
    if central != nil {
      // The owner is bound to this test's engine lifetime, so retirement is the honest end:
      // `close()` alone would leave the lifetime holding a retired member.
      if let lifetime {
        lifetime.invalidate()
        _ = try? await lifetime.waitForRetirement(timeoutMs: 10_000)
        self.lifetime = nil
      }
      _ = try? await Self.settled(central.close())
      central = nil
    }
  }

  func testTwoLinksKeepTheirOwnStateWhenOnePeerEnds() async throws {
    // The relay that carries stage markers may still be attaching, and the first lines this
    // process writes are the ones it loses. The probe burst spends that window on lines the host
    // never reads as stages; only after it does the scene announce its first real marker.
    await warmDiagnosticRelay()

    // A standalone owner refuses to start until a React engine lifetime owns it; the App's RN
    // bridge does that binding in production, and this is the same contract from a test host.
    let lifetime = BlengEngineLifetime()
    self.lifetime = lifetime
    central = HybridBlengCentral()
    XCTAssertTrue(lifetime.bind(central), "the test engine lifetime must own this owner")
    let initialized = try await Self.settled(central.initialize())
    guard case .first = initialized else {
      return XCTFail("the owner must start before the scene can run")
    }
    marker("started")

    let peripherals = try await observe()
    let connections = try await connect(peripherals)
    let sessions = try await discover(connections)
    try await exerciseInterleavedIo(sessions)
    let subscriptions = try await subscribe(sessions)
    try await collectBaselineValues(subscriptions)
    try await disturbFirstLinkAndProveSurvivors(connections, subscriptions)
    try await disconnectSurvivors(connections)
    marker("done")
  }

  // MARK: - Scene stages

  private func observe() async throws -> [any HybridBlengPeripheralSpec] {
    let submission = try central.beginScan(
      options: BlengScanOptions(
        serviceUuids: peers.map(\.serviceUuid),
        namePrefix: nil,
        exactName: nil,
        companyId: nil,
        minRssi: nil,
        manufacturerData: nil,
        android: nil,
        allowDuplicates: false))
    let scan: any HybridBlengScanSpec
    switch submission {
    case .first(let accepted):
      scan = accepted.scan
    case .second(let failure):
      throw SceneFailure(description: "scan submission failed: \(failure)")
    }
    marker("scan-submitted")
    _ = try await Self.settled(scan.waitUntilStarted(timeoutMs: Self.operationDeadlineMs))
    marker("scan-started")

    // Resolve by what each peer advertised, never by arrival order.
    var observed: [Int: any HybridBlengPeripheralSpec] = [:]
    let deadline = Date().addingTimeInterval(Self.settleSeconds)
    while observed.count < peers.count {
      guard Date() < deadline else {
        throw SceneFailure(
          description: "only \(observed.count)/\(peers.count) declared peers advertised")
      }
      switch try await Self.settled(scan.nextEvent(timeoutMs: Self.valueDeadlineMs)) {
      case .first(let event):
        let advertisement = event.advertisement
        let services = advertisement.advertisedServices.map { $0.lowercased() }
        let name = advertisement.localName
        if let index = peers.firstIndex(where: { peer in
          services.contains(peer.serviceUuid.lowercased()) && name == peer.localName
        }), observed[index] == nil {
          observed[index] = advertisement.peripheral
        }
      case .second(let ended):
        throw SceneFailure(description: "scan ended: \(ended)")
      case .third(let failure):
        throw SceneFailure(description: "scan observation failed: \(failure)")
      }
    }
    // Declaration order, not arrival order: the radio's order is not a fact about the run.
    for index in peers.indices {
      marker("advertisement:\(index)")
    }

    switch try scan.beginStop() {
    case .first(let accepted):
      _ = try await Self.settled(accepted.stop.wait(timeoutMs: Self.operationDeadlineMs))
    case .second(let failure):
      throw SceneFailure(description: "stop submission failed: \(failure)")
    }
    marker("scan-stopped")
    return peers.indices.map { observed[$0]! }
  }

  private func connect(
    _ peripherals: [any HybridBlengPeripheralSpec]
  ) async throws -> [any HybridBlengConnectionSpec] {
    var connections: [any HybridBlengConnectionSpec] = []
    for (index, peripheral) in peripherals.enumerated() {
      let connect: any HybridBlengConnectSpec
      switch try peripheral.beginConnect() {
      case .first(let accepted):
        connect = accepted.connect
      case .second(let failure):
        throw SceneFailure(description: "link \(index) connect submission failed: \(failure)")
      }
      let connection: any HybridBlengConnectionSpec
      switch try await Self.settled(connect.wait(timeoutMs: Self.operationDeadlineMs)) {
      case .first(let accepted):
        connection = accepted.connection
      case .second(let failure):
        throw SceneFailure(description: "link \(index) connect failed: \(failure)")
      }
      connections.append(connection)
      marker("connected:\(index)")
    }
    return connections
  }

  private func discover(
    _ connections: [any HybridBlengConnectionSpec]
  ) async throws -> [any HybridBlengGattSessionSpec] {
    var sessions: [any HybridBlengGattSessionSpec] = []
    for (index, connection) in connections.enumerated() {
      let discovery: any HybridBlengGattDiscoverySpec
      switch try connection.beginDiscoverGatt() {
      case .first(let accepted):
        discovery = accepted.discovery
      case .second(let failure):
        throw SceneFailure(description: "link \(index) discovery submission failed: \(failure)")
      }
      let session: any HybridBlengGattSessionSpec
      switch try await Self.settled(discovery.wait(timeoutMs: Self.operationDeadlineMs)) {
      case .first(let accepted):
        session = accepted.session
      case .second(let failure):
        throw SceneFailure(description: "link \(index) discovery failed: \(failure)")
      }
      sessions.append(session)
      marker("gatt:\(index)")
    }
    return sessions
  }

  /// Interleaved I/O: each write must be read back from the link it was sent on.
  private func exerciseInterleavedIo(
    _ sessions: [any HybridBlengGattSessionSpec]
  ) async throws {
    for (index, session) in sessions.enumerated() {
      let characteristic = try characteristic(in: session, uuid: peers[index].readWriteUuid)
      let written = try Self.bytes(fromHex: peers[index].writeHex)
      if index == faultPeer {
        // The host refuses this write inside the peer's own firmware. The App owes two facts:
        // it reported the refusal, and the attribute still holds what it held before the attempt.
        let before = try await readBytes(characteristic, link: index)
        let failure = try await write(characteristic, value: written, link: index)
        guard failure != nil else {
          throw SceneFailure(description: "link \(index) reported a refused write as accepted")
        }
        marker("write-rejected:\(index)")
        let after = try await readBytes(characteristic, link: index)
        XCTAssertEqual(
          after.map { String(format: "%02x", $0) }.joined(),
          before.map { String(format: "%02x", $0) }.joined(),
          "link \(index) changed a value whose write was refused")
        marker("unchanged:\(index)")
        continue
      }
      if let failure = try await write(characteristic, value: written, link: index) {
        throw SceneFailure(description: "link \(index) write failed: \(failure)")
      }
      marker("written:\(index)")

      let value = try await readBytes(characteristic, link: index)
      XCTAssertEqual(
        value.map { String(format: "%02x", $0) }.joined(),
        peers[index].writeHex.lowercased(),
        "link \(index) read back another link's bytes")
      marker("io:\(index)")
    }
  }

  /// Submit one write and return the failure the App reported, if any.
  private func write(
    _ characteristic: any HybridBlengGattCharacteristicSpec,
    value: Data,
    link: Int
  ) async throws -> BlengFailureInfo? {
    let write: any HybridBlengWriteSpec
    switch try characteristic.beginWrite(
      value: ArrayBuffer.copy(data: value), writeType: .withresponse)
    {
    case .first(let accepted):
      write = accepted.write
    case .second(let failure):
      throw SceneFailure(description: "link \(link) write submission failed: \(failure)")
    }
    let receipt = try await Self.settled(write.wait(timeoutMs: Self.operationDeadlineMs))
    return receipt.failure
  }

  /// Read one characteristic, exactly as the App would.
  private func readBytes(
    _ characteristic: any HybridBlengGattCharacteristicSpec,
    link: Int
  ) async throws -> Data {
    let read: any HybridBlengReadSpec
    switch try characteristic.beginRead() {
    case .first(let accepted):
      read = accepted.read
    case .second(let failure):
      throw SceneFailure(description: "link \(link) read submission failed: \(failure)")
    }
    switch try await Self.settled(read.wait(timeoutMs: Self.operationDeadlineMs)) {
    case .first(let accepted):
      return accepted.value.toData(copyIfNeeded: true)
    case .second(let failure):
      throw SceneFailure(description: "link \(link) read failed: \(failure)")
    }
  }

  private func subscribe(
    _ sessions: [any HybridBlengGattSessionSpec]
  ) async throws -> [any HybridBlengSubscriptionSpec] {
    var subscriptions: [any HybridBlengSubscriptionSpec] = []
    for (index, session) in sessions.enumerated() {
      let entry = try characteristic(in: session, uuid: peers[index].updatesUuid)
      let properties = try entry.properties()
      let request: BlengNotificationModeRequest
      if properties.notifiable {
        request = .requirenotification
      } else {
        XCTAssertTrue(properties.indicatable, "\(peers[index].updatesUuid) is neither notify nor indicate")
        request = .requireindication
      }
      let notifications: any HybridBlengSubscriptionSpec
      switch try entry.beginSubscription(mode: request) {
      case .first(let accepted):
        notifications = accepted.notifications
      case .second(let failure):
        throw SceneFailure(description: "link \(index) enable submission failed: \(failure)")
      }
      let receipt: BlengSubscriptionOutcome
      switch try await Self.settled(
        notifications.waitUntilEnabled(timeoutMs: Self.operationDeadlineMs))
      {
      case .first(let accepted):
        receipt = accepted
      case .second(let failure):
        throw SceneFailure(description: "link \(index) enable failed: \(failure)")
      }
      XCTAssertEqual(
        receipt.mode, peers[index].mode,
        "link \(index) selected a mode this peer does not advertise")
      subscriptions.append(notifications)
      marker("subscribed:\(index)")
    }
    return subscriptions
  }

  private func collectBaselineValues(
    _ subscriptions: [any HybridBlengSubscriptionSpec]
  ) async throws {
    // Both links deliver before either is disturbed: the baseline the isolation claim needs.
    for (index, subscription) in subscriptions.enumerated() {
      let value = try await pull(subscription, link: index)
      XCTAssertEqual(
        value, peers[index].valueHex.lowercased(),
        "link \(index) delivered a value the host did not apply")
      marker("value:\(index)")
    }
  }

  private func disturbFirstLinkAndProveSurvivors(
    _ connections: [any HybridBlengConnectionSpec],
    _ subscriptions: [any HybridBlengSubscriptionSpec]
  ) async throws {
    // The host ends only the first peer's link. Nothing on this side asks for it.
    marker("drop-requested:0")
    let ending: BlengEnd
    switch try await Self.settled(connections[0].awaitEnd(timeoutMs: Self.endDeadlineMs)) {
    case .first(let event):
      ending = event.ended
    case .second(let failure):
      throw SceneFailure(description: "link end failed: \(failure)")
    }
    XCTAssertEqual(
      ending.reason, .unrequested,
      "a peer-initiated end must not be attributed to this side")
    marker("dropped:0")

    // Every surviving link is unaffected: each still receives a value the host applies after
    // the other peer ended. Contiguity is the isolation claim, so this is not a one-peer case.
    for index in 1..<peers.count {
      marker("survivor-requested:\(index)")
      let value = try await pull(subscriptions[index], link: index)
      XCTAssertEqual(
        value, peers[index].valueHex.lowercased(),
        "link \(index) lost a value applied after peer 0 ended")
      marker("survivor:\(index)")
    }
  }

  private func disconnectSurvivors(
    _ connections: [any HybridBlengConnectionSpec]
  ) async throws {
    // Each survivor's own end is still its own fact, reported the same way.
    for index in 1..<peers.count {
      let disconnect: any HybridBlengDisconnectSpec
      switch try connections[index].beginDisconnect() {
      case .first(let accepted):
        disconnect = accepted.disconnect
      case .second(let failure):
        throw SceneFailure(description: "link \(index) disconnect submission failed: \(failure)")
      }
      _ = try await Self.settled(disconnect.wait(timeoutMs: Self.operationDeadlineMs))
      marker("disconnected:\(index)")
    }
  }

  // MARK: - Boundaries

  /// One value from this subscription; a timeout is not a delivery and never satisfies a scene.
  private func pull(_ notifications: any HybridBlengSubscriptionSpec, link: Int) async throws
    -> String
  {
    let deadline = Date().addingTimeInterval(Self.settleSeconds)
    while Date() < deadline {
      switch try await Self.settled(
        notifications.nextEvent(timeoutMs: Self.valueDeadlineMs))
      {
      case .first(let event):
        return event.value.toData(copyIfNeeded: true)
          .map { String(format: "%02x", $0) }.joined()
      case .second(let ended):
        throw SceneFailure(description: "link \(link) stream ended: \(ended)")
      case .third(let failure):
        // A caller deadline is not a fact about the link; re-arm like the App does.
        guard failure.failure.domain == .observation else {
          throw SceneFailure(description: "link \(link) observation failed: \(failure)")
        }
      }
    }
    throw SceneFailure(description: "link \(link) delivered nothing the host applied")
  }

  private func characteristic(
    in session: any HybridBlengGattSessionSpec, uuid: String
  ) throws -> any HybridBlengGattCharacteristicSpec {
    let found = try session.services()
      .flatMap { try $0.characteristics() }
      .filter { try $0.uuid().lowercased() == uuid.lowercased() }
    guard found.count == 1 else {
      throw SceneFailure(
        description:
          "the discovered topology does not expose exactly one \(uuid) characteristic: \(found.count)"
      )
    }
    return found[0]
  }

  private func marker(_ name: String) {
    // The same wire shape as the Android lane: one tag token, a delimiter, then the stage. The
    // host's parser requires the delimiter, so a log line that merely mentions the tag is not a
    // stage and cannot advance a fixture.
    NSLog("%@: %@", Self.markerTag, name)
  }

  private func warmDiagnosticRelay() async {
    for index in 0..<Self.probeCount {
      NSLog("%@: probe:%d", Self.probeTag, index)
      if index + 1 < Self.probeCount {
        try? await Task.sleep(nanoseconds: Self.probeGapNanoseconds)
      }
    }
  }

  private static func declaredFault(
    _ environment: [String: String], peers: [Peer]
  ) throws -> Int? {
    // Absent and `none` are the same declared absence. Any other spelling must fail: a run that
    // asked for a fault and got a clean scene would pass without proving the refusal.
    guard let text = environment["BLEHUB_MULTI_FAULT"], !text.isEmpty, text != "none" else {
      return nil
    }
    let parts = text.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
    guard parts.count == 2, parts[0] == "write-reject", let index = Int(parts[1]),
      peers.indices.contains(index)
    else {
      throw SceneFailure(description: "unsupported declared fault: \(text)")
    }
    return index
  }

  private static func declaredPeers(_ environment: [String: String]) throws -> [Peer] {
    guard let payload = environment["BLEHUB_MULTI_PEERS"],
      let data = payload.data(using: .utf8),
      let rows = try JSONSerialization.jsonObject(with: data) as? [[String: Any]]
    else {
      throw XCTSkip("the run environment carries no peer declaration")
    }
    guard rows.count >= 2 else {
      throw SceneFailure(description: "a multi-link scene needs at least two declared peers")
    }
    return try rows.map { row in
      func required(_ key: String) throws -> String {
        guard let value = row[key] as? String, !value.isEmpty else {
          throw SceneFailure(description: "peer declaration lacks \(key)")
        }
        return value
      }
      let mode: BlengNotificationMode
      switch try required("mode") {
      case "notification": mode = .notification
      case "indication": mode = .indication
      default: throw SceneFailure(description: "peer mode must be notification or indication")
      }
      return Peer(
        serviceUuid: try required("serviceUuid"),
        localName: try required("localName"),
        readWriteUuid: try required("readWriteUuid"),
        updatesUuid: try required("updatesUuid"),
        valueHex: try required("valueHex").lowercased(),
        writeHex: try required("writeHex").lowercased(),
        mode: mode)
    }
  }

  private static func bytes(fromHex hex: String) throws -> Data {
    guard hex.count % 2 == 0 else {
      throw SceneFailure(description: "hex payload has an odd length: \(hex)")
    }
    var data = Data()
    var index = hex.startIndex
    while index < hex.endIndex {
      let next = hex.index(index, offsetBy: 2)
      guard let byte = UInt8(hex[index..<next], radix: 16) else {
        throw SceneFailure(description: "hex payload is not hexadecimal: \(hex)")
      }
      data.append(byte)
      index = next
    }
    return data
  }

  private static func settled<Value>(_ promise: Promise<Value>) async throws -> Value {
    try await withCheckedThrowingContinuation { continuation in
      promise.then { value in
        continuation.resume(returning: value)
      }.catch { error in
        continuation.resume(throwing: error)
      }
    }
  }
}
