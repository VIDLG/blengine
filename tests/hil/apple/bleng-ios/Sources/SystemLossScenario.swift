import BleHubUniFfi
import Foundation

// Kept out of the thin deterministic target: only this existing HIL App owns a
// BLeng image. One-shot admission is observed, never assumed or retried. The
// parent owns provider control and any future radio/recovery controller.

private func nativeSnapshot(_ backend: AppleBleBackend) async -> [String: Int] {
    await withCheckedContinuation { continuation in
        backend.diagnosticSnapshotRfResources { continuation.resume(returning: $0) }
    }
}

private func nativeCentralRecreation(_ backend: AppleBleBackend) async -> [String: String] {
    await withCheckedContinuation { continuation in
        backend.diagnosticRecreateCentral { continuation.resume(returning: $0) }
    }
}

private final class SystemLossObservation<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value?
    func set(_ value: Value) {
        lock.lock()
        defer { lock.unlock() }
        self.value = value
    }
    func get() -> Value? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

private struct SystemLossNativeLane {
    let snapshot: AppleGattIoSnapshot?
}

private func publishSystemLossProgress(
    _ runDirectory: URL,
    _ state: String,
    detail: String? = nil
) {
    var document: [String: Any] = [
        "schema_version": 1,
        "state": state,
        "timestamp": Date().timeIntervalSince1970,
    ]
    if let detail { document["detail"] = detail }
    guard let data = try? SystemLossJSON.canonical(document) else { return }
    let temporary = runDirectory.appendingPathComponent(".system-loss-progress.json.tmp")
    let destination = runDirectory.appendingPathComponent("system-loss-progress.json")
    try? data.write(to: temporary, options: .atomic)
    try? FileManager.default.removeItem(at: destination)
    try? FileManager.default.moveItem(at: temporary, to: destination)
}

private func publishSystemLossStage(_ runDirectory: URL, _ name: String, _ state: String) {
    let document: [String: Any] = [
        "schema_version": 1,
        "state": state,
    ]
    guard let data = try? SystemLossJSON.canonical(document) else { return }
    try? data.write(to: runDirectory.appendingPathComponent(name), options: .atomic)
}

private func publishSystemLossError(_ runDirectory: URL, _ name: String, _ error: Error) {
    let document: [String: Any] = [
        "schema_version": 1,
        "state": "app-failed",
        "error": String(describing: error),
    ]
    guard let data = try? SystemLossJSON.canonical(document) else { return }
    try? data.write(to: runDirectory.appendingPathComponent(name), options: .atomic)
}

private func systemLossFailureDiagnostic(
    native: AppleBleBackend,
    runId: String,
    control: Data,
    witness: Data
) async throws -> Data {
    let loss = SystemLossObservation<[String: String]>()
    let resources = SystemLossObservation<[String: Int]>()
    native.diagnosticSnapshotLoss { loss.set($0) }
    native.diagnosticSnapshotRfResources { resources.set($0) }
    let sampled = try await systemLossWait("loss callback diagnostic", seconds: 2) {
        () -> (loss: [String: String], resources: [String: Int])? in
        guard let loss = loss.get(), let resources = resources.get() else { return nil }
        return (loss: loss, resources: resources)
    }
    return try SystemLossJSON.canonical([
        "schema_version": 1,
        "state": "loss-diagnostic",
        "session_id": runId,
        "control_sha256": try SystemLossJSON.digest(control),
        "witness_sha256": try SystemLossJSON.digest(witness),
        "loss": sampled.loss,
        "resources": sampled.resources,
    ] as [String: Any])
}

private func systemLossRecoveryDiagnostic(
    native: AppleBleBackend,
    evidence: ScanEvidence,
    recoveryScan: ScanSessionId,
    runId: String,
    control: Data,
    witness: Data,
    ownerIdentity: Any,
    phase: String,
    recoveryOutcome: String,
    failureReason: String,
    freshOwnerAttempted: Bool = false,
    centralRecreationAttempted: Bool = false,
    centralRecreation: [String: String]? = nil
) async throws -> Data {
    let loss = SystemLossObservation<[String: String]>()
    let resources = SystemLossObservation<[String: Int]>()
    native.diagnosticSnapshotLoss { loss.set($0) }
    native.diagnosticSnapshotRfResources { resources.set($0) }
    let sampled = try await systemLossWait("recovery diagnostic snapshots", seconds: 2) {
        () -> (loss: [String: String], resources: [String: Int])? in
        guard let loss = loss.get(), let resources = resources.get() else { return nil }
        return (loss: loss, resources: resources)
    }
    return try SystemLossJSON.canonical([
        "schema_version": 1,
        "state": "recovery-diagnostic",
        "session_id": runId,
        "control_sha256": try SystemLossJSON.digest(control),
        "witness_sha256": try SystemLossJSON.digest(witness),
        "owner_identity": ownerIdentity,
        "phase": phase,
        "automatic_recovery": "attempted",
        "recovery_outcome": recoveryOutcome,
        "recovery_failure_reason": failureReason,
        "fresh_owner_attempted": freshOwnerAttempted,
        "central_recreation_attempted": centralRecreationAttempted,
        "central_recreation": centralRecreation ?? [:],
        "recovery_scan_id": String(recoveryScan.value),
        "scan_evidence": await evidence.diagnosticCounts(for: recoveryScan),
        "loss": sampled.loss,
        "resources": sampled.resources,
    ] as [String: Any])
}

func systemLossWait<Value>(
    _ label: String, seconds: Int, deadline absolute: ContinuousClock.Instant? = nil,
    check: () async throws -> Value?
) async throws -> Value {
    let clock = ContinuousClock()
    let local = clock.now.advanced(by: .seconds(seconds))
    let deadline = absolute.map { min($0, local) } ?? local
    while clock.now < deadline {
        if let value = try await check() { return value }
        try await Task.sleep(nanoseconds: 25_000_000)
    }
    throw SystemLossFailure.invalid("bounded wait expired: \(label)")
}

private func systemLossHostStop(
    files: SystemLossWitnessFiles, runId: String, control: Data?
) throws {
    guard let stop = try files.read("system-loss-stop.json") else { return }
    let expected: [String: Any] = [
        "schema_version": 1, "session_id": runId, "action": "stop-witness",
        "control_sha256": try control.map { try SystemLossJSON.digest($0) } as Any? ?? NSNull(),
    ]
    try systemLossRequire(try SystemLossJSON.canonical(SystemLossJSON.object(stop)) == SystemLossJSON.canonical(expected),
                          "wrong/stale host stop marker")
    throw SystemLossStopped.host
}

private func systemLossHostCommand(
    files: SystemLossWitnessFiles, name: String, runId: String,
    action: String, control: Data, witness: Data, terminals: Data? = nil
) throws {
    guard let command = try files.read(name) else {
        throw SystemLossFailure.invalid("missing host command \(name)")
    }
    var expected: [String: Any] = [
        "schema_version": 1, "session_id": runId, "action": action,
        "control_sha256": try SystemLossJSON.digest(control),
        "witness_sha256": try SystemLossJSON.digest(witness),
    ]
    if let terminals { expected["terminals_sha256"] = try SystemLossJSON.digest(terminals) }
    try systemLossRequire(
        try SystemLossJSON.canonical(SystemLossJSON.object(command)) == SystemLossJSON.canonical(expected),
        "wrong/stale host command \(name)"
    )
}

func verifyAppleSystemLossWitness(
    arguments: SystemLossArguments,
    runDirectory: URL,
    status: (@Sendable (String) -> Void)? = nil
) async -> String {
    publishSystemLossStage(runDirectory, "function-entered-receipt.json", "function-entered")
    let witness = SystemLossWitness()
    publishSystemLossStage(runDirectory, "witness-created-receipt.json", "witness-created")
    let attempt = SystemLossOneShotAttempt(witness: witness)
    publishSystemLossStage(runDirectory, "attempt-created-receipt.json", "attempt-created")
    var files: SystemLossWitnessFiles?
    var supervisor: AppleRustSupervisorOwner?
    var wrapper: SystemLossWitnessBackend?
    let eventReaderError = SystemLossObservation<String>()
    let witnessActive = SystemLossObservation<Bool>()
    witnessActive.set(true)
    var primary: String?
    var cleanupErrors: [String] = []
    var stopReason = "failure"
    var basePublished = false
    var armedObserved = false
    var nativeLaneObserved = false
    var pendingPublished = false
    var providerHeldObserved = false
    var lossAuthorized = false
    var recoveryObserved = false
    var automaticRecoveryOutcome = "not-attempted"
    var recoveryFailureReason: String?
    var freshOwnerAttempted = false
    var control: Data?
    do {
        let output: SystemLossWitnessFiles
        publishSystemLossStage(runDirectory, "files-init-started-receipt.json", "files-init-started")
        do {
            output = try SystemLossWitnessFiles(directory: runDirectory)
        } catch {
            publishSystemLossError(runDirectory, "files-init-failure-receipt.json", error)
            throw error
        }
        files = output
        publishSystemLossStage(runDirectory, "files-init-complete-receipt.json", "files-init-complete")
        publishSystemLossProgress(runDirectory, "started")
        let service = BluetoothUuid(value: arguments.serviceUuid)
        let evidence = ScanEvidence(service: service)
        let evidenceRouter = ScanEvidenceRouter(evidence)
        let nativeHolder = SystemLossObservation<AppleBleBackend>()
        let wrapperHolder = SystemLossObservation<SystemLossWitnessBackend>()
        let recoveryTarget = arguments.characteristicUuid.map {
            AppleRustSupervisorHILAdapter.recoveryTarget(
                serviceUuid: arguments.serviceUuid,
                characteristicUuid: $0,
                expectedValue: Data([0xef]),
                scanNamePrefix: arguments.namePrefix
            )
        }
        if arguments.active {
            try systemLossRequire(recoveryTarget != nil, "active system-loss recovery requires an explicit characteristic UUID")
        }
        let sessionSupervisor = try AppleRustSupervisorOwner(
            queueLabel: "dev.blehub.bleng.ios-system-loss-witness",
            recoveryTarget: recoveryTarget,
            allowCentralRecreation: true,
            allowFreshOwner: true,
            eventSink: { event in
                if witnessActive.get() != false {
                    witness.record(event)
                }
                evidenceRouter.record(event)
            },
            backendFactory: { queueLabel in
                let native = try AppleBleBackend(queueLabel: queueLabel)
                nativeHolder.set(native)
                if witnessActive.get() == true {
                    let backend = SystemLossWitnessBackend(delegate: native, witness: witness)
                    wrapperHolder.set(backend)
                    return ApplePlatformBackendLease(
                        backend: backend,
                        resourceSnapshot: { await nativeSnapshot(native) },
                        recreateCentral: { await nativeCentralRecreation(native) }
                    )
                }
                // Fresh-owner events must never be rebound to the old witness.
                return ApplePlatformBackendLease(
                    backend: native,
                    resourceSnapshot: { await nativeSnapshot(native) },
                    recreateCentral: { await nativeCentralRecreation(native) }
                )
            }
        )
        supervisor = sessionSupervisor
        let hil = AppleRustSupervisorHILAdapter(owner: sessionSupervisor)
        publishSystemLossStage(runDirectory, "supervisor-started-receipt.json", "supervisor-started")
        let start: ForeignOwnerStart
        do {
            start = try await sessionSupervisor.start()
        } catch {
            publishSystemLossError(runDirectory, "supervisor-failure-receipt.json", error)
            throw error
        }
        publishSystemLossStage(runDirectory, "supervisor-complete-receipt.json", "supervisor-complete")
        publishSystemLossProgress(runDirectory, "initialized")
        let snapshot = start.adapterSnapshot
        let current = try hil.activeOwnerForHost()
        guard let native = nativeHolder.get(), let backend = wrapperHolder.get() else {
            throw SystemLossFailure.invalid("supervisor did not publish its backend lease")
        }
        try witness.bindSessionIdentity(
            ownerId: start.context.ownerId,
            backendId: start.context.backendId
        )
        wrapper = backend
        let initializeCommand = CommandId(value: start.initializationCommandId)
        try systemLossRequire(snapshot.state == .poweredOn && snapshot.scanAuthorization == .authorized
            && snapshot.connectionAuthorization == .authorized, "powered-on/authorized Central required; no radio or permission mutation")
        let scan = try await current.startScan(
            options: ScanOptions(serviceUuids: [service], namePrefix: arguments.namePrefix, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false),
            timeoutMs: 10_000
        )
        let advertisement: AdvertisementObservation = try await systemLossWait("explicit service/name advertisement", seconds: 30) {
            guard let candidate = await evidence.advertisement(for: scan),
                  candidate.localName?.hasPrefix(arguments.namePrefix) == true else { return nil }
            return candidate
        }
        publishSystemLossProgress(runDirectory, "advertisement", detail: String(advertisement.peripheralId.value))
        try await current.stopScan(session: scan, timeoutMs: 10_000)
        _ = try await systemLossWait("scan stop event", seconds: 5) {
            await evidence.completedStop(for: scan) ? true : nil
        }
        let connection = try await current.connect(peripheral: advertisement.peripheralId, timeoutMs: 20_000)
        _ = try await systemLossWait("ordered connection events", seconds: 5) {
            await evidence.connected(connection) ? true : nil
        }
        publishSystemLossProgress(runDirectory, "connected", detail: String(connection.value))
        let database = try await current.discoverGatt(connection: connection, timeoutMs: 25_000)
        publishSystemLossProgress(runDirectory, "gatt-discovered", detail: String(database.gattSessionId.value))
        let observed = try await systemLossWait("public discovered database", seconds: 5) {
            await evidence.database(connection: connection, session: database.gattSessionId)
        }
        try systemLossRequire(database == observed, "public event database differs from typed discovery result")
        let route = try SystemLossRoute(database: database, serviceUuid: arguments.serviceUuid)
        let read = try current.beginReadCharacteristic(connection: connection, session: database.gattSessionId, characteristic: route.read.id)
        let before = try await read.wait(timeoutMs: 10_000)
        try systemLossRequire(before == arguments.before, "actual before bytes differ from explicit intent")
        _ = try await systemLossWait("public before read", seconds: 5) {
            witness.observedRead(command: read.commandId(), route: route, value: before) ? true : nil
        }
        var baselineCommand: CommandId?
        var baselineSubscription: SubscriptionId?
        if arguments.scenario == .disableWrite {
            let enable = try current.beginEnableNotifications(
                connection: connection, session: database.gattSessionId, characteristic: route.updates.id, mode: .requireNotification
            )
            let subscription = try await enable.wait(timeoutMs: 15_000).subscription
            let enabled = try await systemLossWait("public baseline enabled", seconds: 5) {
                await evidence.notificationEnabled(connection: connection, session: database.gattSessionId,
                                                   subscription: subscription, characteristic: route.updates.id, mode: .notification)
            }
            try systemLossRequire(enabled.command == enable.commandId(), "baseline public operation commandId drift")
            let value = try await systemLossWait("public baseline EF value", seconds: 5) {
                await evidence.notificationValue(after: enabled.index, connection: connection, session: database.gattSessionId,
                                                 subscription: subscription, characteristic: route.updates.id)
            }
            try systemLossRequire(value.value == Data([0xef]), "baseline value must be real EF")
            baselineCommand = enable.commandId()
            baselineSubscription = subscription
        }
        try systemLossRequire(!(await evidence.hasPostStopAdvertisement()), "advertisement after scan stopped")
        let setup = try witness.prepare(
            scenario: arguments.scenario, sessionId: arguments.runId, serviceUuid: arguments.serviceUuid, database: database,
            initializeCommand: initializeCommand, readCommand: read.commandId(), before: before, requested: arguments.requested,
            baselineCommand: baselineCommand, baselineSubscription: baselineSubscription
        )
        try output.publishSetup(setup)
        publishSystemLossProgress(runDirectory, "setup-exported")
        NSLog("system-loss witness setup exported; waiting for control/ARM before one-shot pending attempt")
        // This is the real production SPI, not an internal/capacity stand-in. An
        // idle observation is diagnostic only and never emitted as queue-pending.
        let lane = SystemLossObservation<SystemLossNativeLane>()
        native.diagnosticSnapshotGattIo(connectionId: connection) { lane.set(SystemLossNativeLane(snapshot: $0)) }
        let sampled = try await systemLossWait("production native lane snapshot", seconds: 2) { lane.get() }
        nativeLaneObserved = sampled.snapshot != nil
        let incoming = try await systemLossWait("host immutable control", seconds: 60) {
            try output.verify()
            try systemLossHostStop(files: output, runId: arguments.runId, control: nil)
            if let error = eventReaderError.get() {
                throw SystemLossFailure.invalid("event reader: \(error)")
            }
            if let error = witness.observationError() { throw SystemLossFailure.invalid(error) }
            return try output.read("system-loss-control.json")
        }
        let base = try witness.bindControl(incoming)
        try output.freezeControl(incoming)
        control = incoming
        try output.publishWitness(base)
        basePublished = true
        // Base exists before the host ARM step: no future command/subscription
        // getters and no circular prepare-control -> pending-witness dependency.
        _ = try await systemLossWait("matching provider ARMED", seconds: 30) {
            try output.verify()
            try systemLossHostStop(files: output, runId: arguments.runId, control: incoming)
            if let error = eventReaderError.get() {
                throw SystemLossFailure.invalid("event reader: \(error)")
            }
            if let error = witness.observationError() { throw SystemLossFailure.invalid(error) }
            guard let data = try output.read("system-loss-physical.json") else { return nil as Bool? }
            let physical = try SystemLossJSON.object(data)
            // Prepared is the only legitimate nonterminal predecessor. Even while
            // waiting it must belong to this control/session, not an old provider.
            try systemLossRequire(physical["session_id"] as? String == arguments.runId
                && physical["control_sha256"] as? String == SystemLossJSON.digest(incoming), "stale provider physical document")
            if physical["state"] as? String == "prepared" { return nil }
            try witness.observeArmed(data)
            return true
        }
        armedObserved = true
        // One absolute budget covers head admission, sibling admission, fresh
        // native sampling, provider HELD and witness retention. Never reset it on
        // progress; leave margin before the provider's 8s/native 10s watchdogs.
        let deadline = ContinuousClock.now.advanced(by: .seconds(6))
        func checkAttempt() throws {
            try output.verify()
            try systemLossHostStop(files: output, runId: arguments.runId, control: incoming)
            if let error = eventReaderError.get() {
                throw SystemLossFailure.invalid("event reader: \(error)")
            }
            if let error = witness.observationError() { throw SystemLossFailure.invalid(error) }
        }
        func submitWrite() throws -> SystemLossPendingOperation {
            let operation = try current.beginWriteCharacteristic(
                connection: connection, session: database.gattSessionId, characteristic: route.read.id,
                value: arguments.requested, writeType: .withResponse
            )
            return SystemLossPendingOperation(commandId: operation.commandId()) { timeout in
                try await operation.wait(timeoutMs: timeout)
            }
        }
        func submitCcc() throws -> SystemLossPendingOperation {
            if arguments.scenario == .writeEnable {
                let operation = try current.beginEnableNotifications(
                    connection: connection, session: database.gattSessionId, characteristic: route.updates.id,
                    mode: .requireNotification
                )
                return SystemLossPendingOperation(commandId: operation.commandId()) { timeout in
                    _ = try await operation.wait(timeoutMs: timeout)
                }
            }
            guard let baselineSubscription else { throw SystemLossFailure.invalid("observed baseline subscription required") }
            let operation = try current.beginDisableNotifications(
                connection: connection, session: database.gattSessionId, subscription: baselineSubscription
            )
            return SystemLossPendingOperation(commandId: operation.commandId()) { timeout in
                try await operation.wait(timeoutMs: timeout)
            }
        }
        // Current provider contract has no head-held pre-marker. Use the explicit
        // parent-authorized fallback: actual backend head success, then ONE sibling
        // submission. Its wait task preserves precise Busy/other admission errors.
        // No backend-success getter or elapsed sleep is treated as actor-ack proof.
        _ = try await attempt.run(deadline: deadline,
            beginHead: {
                NSLog("system-loss one-shot: begin-head-start scenario=\(arguments.scenario.rawValue)")
                let operation = arguments.scenario == .writeEnable ? try submitWrite() : try submitCcc()
                NSLog("system-loss one-shot: begin-head-return command=\(operation.commandId.value)")
                return operation
            }, beginSibling: {
                NSLog("system-loss one-shot: begin-sibling-start scenario=\(arguments.scenario.rawValue)")
                let operation = arguments.scenario == .writeEnable ? try submitCcc() : try submitWrite()
                NSLog("system-loss one-shot: begin-sibling-return command=\(operation.commandId.value)")
                return operation
            },
            check: checkAttempt,
            snapshot: {
                let observation = SystemLossObservation<SystemLossNativeLane>()
                native.diagnosticSnapshotGattIo(connectionId: connection) {
                    observation.set(SystemLossNativeLane(snapshot: $0))
                }
                let sampled = try await systemLossWait("fresh production native FIFO", seconds: 6, deadline: deadline) { observation.get() }
                nativeLaneObserved = sampled.snapshot != nil
                return sampled.snapshot
            }, publish: { pending in
                try output.publishWitness(pending)
                pendingPublished = true
                status?("BLeng: system-loss pending")
            })
        // Export first so provider.observe() can validate/bind it without a cycle.
        // This does NOT authorize OFF: host authorization requires its own fresh
        // HELD recheck. No radio mutation or recovery action exists in this App path.
        _ = try await systemLossWait("provider HELD binding", seconds: 6, deadline: deadline) {
            try checkAttempt()
            try attempt.checkOperations()
            guard let data = try output.read("system-loss-physical.json") else { return nil as Bool? }
            let physical = try SystemLossJSON.object(data)
            try systemLossRequire(physical["session_id"] as? String == arguments.runId
                && physical["control_sha256"] as? String == SystemLossJSON.digest(incoming), "stale provider HELD session/control")
            if physical["state"] as? String == "armed" { return nil }
            try witness.observeHeldPending(data)
            return true
        }
        providerHeldObserved = true
        if arguments.active {
            // This command only authorizes the App's loss-terminal observation;
            // the host performs the external OFF action after this phase witness.
            _ = try await systemLossWait("host loss authorization", seconds: 300) {
                try checkAttempt()
                guard let command = try output.read("system-loss-authorize.json") else { return nil as Bool? }
                let expected: [String: Any] = [
                    "schema_version": 1, "session_id": arguments.runId, "action": "authorize-loss",
                    "control_sha256": try SystemLossJSON.digest(incoming),
                    "witness_sha256": try SystemLossJSON.digest(try witness.witness()),
                ]
                try systemLossRequire(
                    try SystemLossJSON.canonical(SystemLossJSON.object(command)) == SystemLossJSON.canonical(expected),
                    "wrong/stale loss authorization command"
                )
                return true
            }
            try attempt.authorizeLoss()
            lossAuthorized = true
            let lossStarted = try SystemLossJSON.canonical([
                "schema_version": 1,
                "state": "loss-started",
                "session_id": arguments.runId,
                "control_sha256": try SystemLossJSON.digest(incoming),
                "witness_sha256": try SystemLossJSON.digest(try witness.witness()),
                "identity": try SystemLossJSON.object(try witness.witness())["identity"] ?? NSNull(),
                "authorization": "host-authorized",
            ] as [String: Any])
            try output.publishLossStarted(lossStarted)
            let terminals: [[String: Any]]
            do {
                terminals = try await systemLossWait("loss operation terminals", seconds: 60) {
                    try checkAttempt()
                    return try? attempt.terminalResults()
                }
            } catch {
                do {
                    let diagnostic = try await systemLossFailureDiagnostic(
                        native: native,
                        runId: arguments.runId,
                        control: incoming,
                        witness: try witness.witness()
                    )
                    try output.publishDiagnostic(diagnostic)
                } catch {
                    NSLog("system-loss diagnostic export failed: \(error)")
                }
                throw error
            }
            let terminalDocument = try SystemLossJSON.canonical([
                "schema_version": 1, "state": "loss-terminals", "session_id": arguments.runId,
                "control_sha256": try SystemLossJSON.digest(incoming),
                "witness_sha256": try SystemLossJSON.digest(try witness.witness()),
                "identity": try SystemLossJSON.object(try witness.witness())["identity"] ?? NSNull(),
                "terminals": terminals,
            ] as [String: Any])
            try output.publishLossTerminals(terminalDocument)
            let expectedOldOperationIds = terminals.compactMap { $0["command_id"] as? String }
            let oldLink = baselineSubscription.map {
                ForeignRecoveryLink(
                    connectionId: String(connection.value),
                    gattSessionId: String(database.gattSessionId.value),
                    subscriptionId: String($0.value)
                )
            }
            try hil.configureOldOperationBarrier(
                oldLink: oldLink,
                expectedOldOperationIds: expectedOldOperationIds
            )
            let restore = try await systemLossWait("host radio restoration", seconds: 300) {
                try checkAttempt()
                guard let command = try output.read("system-loss-restore.json") else { return nil as Bool? }
                let commandObject = try SystemLossJSON.object(command)
                guard let receiptHash = commandObject["disconnect_receipt_sha256"] as? String else {
                    throw SystemLossFailure.invalid("missing disconnect receipt digest")
                }
                try systemLossRequire(receiptHash.count == 64
                    && receiptHash.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) },
                    "malformed disconnect receipt digest")
                let expected: [String: Any] = [
                    "schema_version": 1, "session_id": arguments.runId, "action": "restore-on",
                    "control_sha256": try SystemLossJSON.digest(incoming),
                    "witness_sha256": try SystemLossJSON.digest(try witness.witness()),
                    "terminals_sha256": try SystemLossJSON.digest(terminalDocument),
                    // The host must bind this relay to its independently
                    // verified retained provider disconnect receipt.
                    "disconnect_receipt_sha256": receiptHash,
                ]
                try systemLossRequire(
                    try SystemLossJSON.canonical(SystemLossJSON.object(command)) == SystemLossJSON.canonical(expected),
                    "wrong/stale radio restoration command"
                )
                return true
            }
            _ = restore
            automaticRecoveryOutcome = "attempted"
            witnessActive.set(false)
            let recoveryOwnerIdentity = try SystemLossJSON.object(try witness.witness())["identity"] ?? NSNull()
            let activeContext = try hil.activeContextForHost()
            let freshContext = ForeignOwnerContext(
                ownerId: UUID().uuidString,
                backendId: UUID().uuidString,
                ownerGeneration: activeContext.ownerGeneration + 1,
                recoveryAttempt: activeContext.recoveryAttempt
            )
            do {
                let runner = RustSupervisorRecoveryRunner(owner: hil)
                let plan = try await runner.run(signal: .radioLost, freshOwnerContext: freshContext)
                guard let outcome = plan.outcome else {
                    throw SystemLossFailure.invalid("Rust recovery returned no terminal outcome")
                }
                guard case let .recovered(context, mode, link) = outcome else {
                    throw SystemLossFailure.invalid("Rust recovery failed: \(outcome)")
                }
                guard let connectionValue = UInt64(link.connectionId),
                      let sessionValue = UInt64(link.gattSessionId),
                      let subscriptionValue = UInt64(link.subscriptionId) else {
                    throw SystemLossFailure.invalid("Rust recovery returned a non-numeric link")
                }
                let recoveredConnection = ConnectionId(value: connectionValue)
                let recoveredSession = GattSessionId(value: sessionValue)
                let recoveredSubscription = SubscriptionId(value: subscriptionValue)
                let recoveryEnabled = try await systemLossWait("recovery notification enabled", seconds: 10) {
                    await evidence.notificationEnabled(
                        connection: recoveredConnection,
                        session: recoveredSession,
                        subscription: recoveredSubscription,
                        mode: .notification
                    )
                }
                let recoveryValue = try await systemLossWait("recovery EF notification", seconds: 60) {
                    await evidence.notificationValue(
                        after: recoveryEnabled.index,
                        connection: recoveredConnection,
                        session: recoveredSession,
                        subscription: recoveredSubscription
                    )
                }
                try systemLossRequire(recoveryValue.value == Data([0xef]), "recovery notification was not EF")
                let fresh = mode == .freshOwner
                freshOwnerAttempted = fresh
                let recoveryDocument = try SystemLossJSON.canonical([
                    "schema_version": 1,
                    "state": fresh ? "fresh-owner-recovered" : "same-owner-recovered",
                    "session_id": arguments.runId,
                    "control_sha256": try SystemLossJSON.digest(incoming),
                    "terminals_sha256": try SystemLossJSON.digest(terminalDocument),
                    "identity": try SystemLossJSON.object(try witness.witness())["identity"] ?? NSNull(),
                    "new_identity": [
                        "owner_id": context.ownerId,
                        "backend_id": context.backendId,
                    ],
                    "new_link": [
                        "connection_id": link.connectionId,
                        "gatt_session_id": link.gattSessionId,
                        "subscription_id": link.subscriptionId,
                    ],
                ] as [String: Any])
                try output.publishRecovery(recoveryDocument)
                recoveryObserved = true
                automaticRecoveryOutcome = "succeeded"
                stopReason = fresh ? "fresh-owner-recovered" : "same-owner-recovered"
            } catch {
                automaticRecoveryOutcome = "platform-timeout"
                recoveryFailureReason = String(describing: error)
                guard let diagnosticNative = nativeHolder.get() else {
                    throw SystemLossFailure.invalid("supervisor backend lease unavailable for recovery diagnostic")
                }
                let diagnostic = try await systemLossRecoveryDiagnostic(
                    native: diagnosticNative,
                    evidence: evidence,
                    recoveryScan: scan,
                    runId: arguments.runId,
                    control: incoming,
                    witness: try witness.witness(),
                    ownerIdentity: recoveryOwnerIdentity,
                    phase: "rust-recovery-runner",
                    recoveryOutcome: automaticRecoveryOutcome,
                    failureReason: recoveryFailureReason ?? "recovery failed",
                    freshOwnerAttempted: freshOwnerAttempted,
                    centralRecreationAttempted: false,
                    centralRecreation: nil
                )
                try output.publishRecoveryDiagnostic(diagnostic)
                stopReason = "platform-timeout"
            }
        } else {
            while ContinuousClock.now < deadline {
                try checkAttempt()
                try attempt.checkOperations()
                guard let data = try output.read("system-loss-physical.json") else {
                    throw SystemLossFailure.invalid("provider HELD document disappeared")
                }
                try witness.observeHeldPending(data)
                try await Task.sleep(nanoseconds: 5_000_000)
            }
            stopReason = "witness-deadline"
        }
    } catch SystemLossStopped.host {
        stopReason = "host-stop-witness-only"
    } catch {
        primary = String(describing: error)
        stopReason = attempt.state == "failed" ? "one-shot-attempt-failed" : "witness-observation-failed"
    }
    witness.stop()
    if primary == nil, let error = witness.observationError() { primary = error }
    if primary == nil, let error = eventReaderError.get() {
        primary = "event reader: \(error)"
    }
    do { try files?.verify() } catch {
        if primary == nil { primary = String(describing: error) }
        else { cleanupErrors.append("evidence integrity: \(error)") }
    }
    attempt.stop(failure: primary)
    if let supervisor {
        do {
            try await supervisor.close()
        } catch { cleanupErrors.append("session supervisor close: \(error)") }
    }
    if let backend = wrapper, backend.counts().dispose == 0 {
        // Covers owner creation/assembly rejection before it acquired the backend.
        // The wrapper serializes this with any late canonical cleanup and forwards once.
        let result = SystemLossObservation<Result<Void, Error>>()
        Task {
            do { try backend.dispose(); result.set(.success(())) }
            catch { result.set(.failure(error)) }
        }
        do {
            let outcome = try await systemLossWait("unbound backend dispose", seconds: 15) { result.get() }
            try outcome.get()
        } catch { cleanupErrors.append("unbound backend dispose: \(error)") }
    }
    attempt.retireWaiters()
    let counts = wrapper?.counts() ?? (bind: 0, initialize: 0, dispose: 0)
    let pendingState = pendingPublished ? "exported" : (armedObserved ? "not-observed" : "not-evaluated")
    let controlHash = control.flatMap { try? SystemLossJSON.digest($0) }
    let status: [String: Any] = [
        "schema_version": 1, "scope": "native-witness-only", "session_id": arguments.runId, "case": arguments.scenario.rawValue,
        "state": primary == nil ? "stopped" : "failed", "stop_reason": stopReason,
        "pending_witness": pendingState, "loss_authorized": lossAuthorized,
        "same_hub_recovery": recoveryObserved ? "observed" : "not-evaluated",
        "automatic_recovery": automaticRecoveryOutcome == "not-attempted" ? "not-attempted" : "attempted",
        "recovery_outcome": automaticRecoveryOutcome,
        "recovery_failure_reason": recoveryFailureReason as Any? ?? NSNull(),
        "rf": "not-evaluated", "gate": "pending",
        "remaining_gaps": recoveryObserved ? ["radio-host-controller"] : ["radio-host-controller", "same-hub-recovery"],
        "attempt_policy": "one-shot-after-backend-return; admission may fail; no retry",
        "head_held_before_sibling": "not-observed; current-provider fallback",
        "attempt": attempt.status(), "base_witness_published": basePublished, "provider_armed_observed": armedObserved,
        "provider_held_observed": providerHeldObserved,
        "native_lane_observed": nativeLaneObserved, "control_sha256": controlHash as Any? ?? NSNull(),
        "bind_count": counts.bind, "initialize_count": counts.initialize, "dispose_count": counts.dispose,
        "primary_error": primary as Any? ?? NSNull(), "native_error": witness.observationError() as Any? ?? NSNull(),
        "cleanup_errors": cleanupErrors,
    ]
    do { try files?.finish(status) } catch { cleanupErrors.append("terminal export: \(error)") }
    return [
        "apple_bleng_system_loss_witness=\(primary == nil ? "stopped" : "failed")",
        "scope=native-witness-only", "case=\(arguments.scenario.rawValue)", "stop_reason=\(stopReason)",
        "pending_witness=\(pendingState)", "provider_held_observed=\(providerHeldObserved)",
        "same_hub_recovery=\(recoveryObserved ? "observed" : "not-evaluated")",
        "automatic_recovery=\(automaticRecoveryOutcome == "not-attempted" ? "not-attempted" : "attempted")",
        "recovery_outcome=\(automaticRecoveryOutcome)",
        "recovery_failure_reason=\(recoveryFailureReason ?? "none")",
        "rf=not-evaluated", "gate=pending",
        "remaining_gaps=\(recoveryObserved ? "radio-host-controller" : "radio-host-controller,same-hub-recovery")", "primary_error=\(primary ?? "none")", "cleanup_errors=\(cleanupErrors)",
    ].joined(separator: "\n") + "\n"
}
