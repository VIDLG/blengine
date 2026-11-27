import BleHubUniFfi
import Foundation

private final class CentralRecoverySpikeObservation<Value>: @unchecked Sendable {
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

private struct CentralRecoverySpikeSnapshot {
    let adapterState: String
    let scanAuthorization: String
    let connectionAuthorization: String
    let resources: [String: Int]
}

private func centralRecoverySpikeWait<T>(
    _ label: String,
    seconds: Int,
    check: () async -> T?
) async throws -> T {
    for _ in 0..<(seconds * 10) {
        if let value = await check() { return value }
        try await Task.sleep(nanoseconds: 100_000_000)
    }
    throw ScanProbeFailure.invalid("timed out waiting for \(label)")
}

private func centralRecoverySpikeResources(
    _ backend: AppleBleBackend
) async -> [String: Int] {
    let observation = CentralRecoverySpikeObservation<[String: Int]>()
    backend.diagnosticSnapshotRfResources { observation.set($0) }
    return (try? await centralRecoverySpikeWait("native resource snapshot", seconds: 2) {
        observation.get()
    }) ?? [:]
}

private func centralRecoverySpikeDocument(
    runId: String,
    ownerId: String,
    backendId: String,
    snapshot: CentralRecoverySpikeSnapshot,
    firstSession: ScanSessionId?,
    firstAdvertisement: AdvertisementObservation?,
    firstCount: Int,
    recreation: [String: String]?,
    secondSession: ScanSessionId?,
    secondAdvertisement: AdvertisementObservation?,
    secondCount: Int,
    outcome: String,
    failureReason: String?
) throws -> Data {
    var object: [String: Any] = [
        "schema_version": 1,
        "run_id": runId,
        "owner_id": ownerId,
        "backend_id": backendId,
        "diagnostic_scope": "authorized-central-recovery-spike",
        "production_gate_claim": "none",
        "automatic_recovery": "not-evaluated",
        "adapter_state": snapshot.adapterState,
        "scan_authorization": snapshot.scanAuthorization,
        "connection_authorization": snapshot.connectionAuthorization,
        "first_scan_session_id": firstSession.map { String($0.value) } ?? NSNull(),
        "first_matching_advertisements": firstCount,
        "first_peripheral_id": firstAdvertisement.map { String($0.peripheralId.value) } ?? NSNull(),
        "central_recreation": recreation ?? [:],
        "second_scan_session_id": secondSession.map { String($0.value) } ?? NSNull(),
        "second_matching_advertisements": secondCount,
        "second_peripheral_id": secondAdvertisement.map { String($0.peripheralId.value) } ?? NSNull(),
        "outcome": outcome,
        "native_resources": snapshot.resources,
    ]
    if let failureReason {
        object["failure_reason"] = failureReason
    }
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    return data
}

private func centralRecoverySpikeWrite(
    directory: URL,
    data: Data
) throws {
    try data.write(
        to: directory.appendingPathComponent("central-recovery-spike.json"),
        options: .atomic
    )
}

/// Non-destructive native recovery spike. It intentionally stops before connect/GATT:
/// the result only answers whether a fresh CoreBluetooth scan receives callbacks after
/// replacing the native CBCentralManager. It is not a system-loss or RF gate.
func verifyAppleCentralRecoverySpike(
    serviceUuid: String,
    runId: String,
    runDirectory: URL,
    status: (@Sendable (String) -> Void)? = nil
) async throws -> String {
    guard let uuid = UUID(uuidString: serviceUuid) else {
        throw ScanProbeFailure.invalid("invalid fixture service UUID")
    }
    let service = BluetoothUuid(value: uuid.uuidString.lowercased())
    let ownerId = UUID().uuidString.lowercased()
    let backendId = UUID().uuidString.lowercased()
    let backend = try AppleBleBackend(queueLabel: "dev.blehub.bleng.ios-central-recovery-spike")

    do {
        let owner = try Bleng()
        do {
            try await owner.assembleBackend(backend: backend)
            let initial = try await owner.initialize(timeoutMs: 30_000)
            guard initial.state == .poweredOn, initial.scanAuthorization == .authorized else {
                throw ScanProbeFailure.invalid("CoreBluetooth is not powered on and scan-authorized")
            }
            let evidence = ScanEvidence(service: service)
            let reader = Task {
                while let event = try await owner.nextEvent(timeoutMs: 86_400_000) {
                    await evidence.record(event)
                }
            }
            let initialResources = await centralRecoverySpikeResources(backend)
            let initialSnapshot = CentralRecoverySpikeSnapshot(
                adapterState: String(describing: initial.state),
                scanAuthorization: String(describing: initial.scanAuthorization),
                connectionAuthorization: String(describing: initial.connectionAuthorization),
                resources: initialResources
            )
            var firstSession: ScanSessionId?
            var firstAdvertisement: AdvertisementObservation?
            var recreation: [String: String]?
            var secondSession: ScanSessionId?
            var secondAdvertisement: AdvertisementObservation?
            var outcome = "failed"
            var failureReason: String?

            do {
                status?("BLeng: central recovery spike first scan")
                firstSession = try await owner.startScan(
                    options: ScanOptions(serviceUuids: [service], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false),
                    timeoutMs: 10_000
                )
                firstAdvertisement = try await centralRecoverySpikeWait(
                    "first scan advertisement", seconds: 30
                ) {
                    guard let firstSession,
                          let advertisement = await evidence.advertisement(for: firstSession)
                    else { return nil }
                    return advertisement
                }
                guard let firstSession, firstAdvertisement != nil else {
                    throw ScanProbeFailure.invalid("first scan session was not retained")
                }
                try await owner.stopScan(session: firstSession, timeoutMs: 10_000)
                _ = try await centralRecoverySpikeWait("first scan stop", seconds: 5) {
                    await evidence.completedStop(for: firstSession)
                }

                status?("BLeng: central recovery spike recreating Central")
                let recreationObservation = CentralRecoverySpikeObservation<[String: String]>()
                backend.diagnosticRecreateCentral { recreationObservation.set($0) }
                do {
                    recreation = try await centralRecoverySpikeWait(
                        "native Central recreation callback", seconds: 10
                    ) {
                        recreationObservation.get()
                    }
                } catch {
                    outcome = "platform-timeout"
                    failureReason = "central-state-callback-timeout"
                }

                if let recreation, recreation["recreated"] == "true" {
                    status?("BLeng: central recovery spike second scan")
                    secondSession = try await owner.startScan(
                        options: ScanOptions(serviceUuids: [service], namePrefix: nil, exactName: nil, companyId: nil, minRssi: nil, manufacturerData: nil, android: nil, allowDuplicates: false),
                        timeoutMs: 10_000
                    )
                    do {
                        secondAdvertisement = try await centralRecoverySpikeWait(
                            "second scan advertisement callback", seconds: 30
                        ) {
                            guard let secondSession,
                                  let advertisement = await evidence.advertisement(for: secondSession)
                            else { return nil }
                            return advertisement
                        }
                        outcome = secondAdvertisement == nil
                            ? "platform-timeout" : "advertisement-observed"
                        if secondAdvertisement == nil {
                            failureReason = "no-advertisement-callback"
                        }
                    } catch {
                        outcome = "platform-timeout"
                        failureReason = "no-advertisement-callback"
                    }
                    if let secondSession {
                        try? await owner.stopScan(session: secondSession, timeoutMs: 10_000)
                    }
                } else if failureReason == nil {
                    outcome = "refused"
                    failureReason = recreation?["reason"] ?? "central-recreation-refused"
                }
            } catch {
                try? await owner.shutdown()
                reader.cancel()
                _ = try? await reader.value
                throw error
            }

            let firstCount: Int
            if let firstSession {
                firstCount = await evidence.diagnosticCounts(for: firstSession)["matching_advertisements"] ?? 0
            } else {
                firstCount = 0
            }
            let secondCount: Int
            if let secondSession {
                secondCount = await evidence.diagnosticCounts(for: secondSession)["matching_advertisements"] ?? 0
            } else {
                secondCount = 0
            }
            let data = try centralRecoverySpikeDocument(
                runId: runId,
                ownerId: ownerId,
                backendId: backendId,
                snapshot: initialSnapshot,
                firstSession: firstSession,
                firstAdvertisement: firstAdvertisement,
                firstCount: firstCount,
                recreation: recreation,
                secondSession: secondSession,
                secondAdvertisement: secondAdvertisement,
                secondCount: secondCount,
                outcome: outcome,
                failureReason: failureReason
            )
            try centralRecoverySpikeWrite(directory: runDirectory, data: data)
            try await owner.shutdown()
            _ = try? await reader.value

            var lines = [
                "apple_bleng_corebluetooth_central_recovery_spike=attempted",
                "central_recovery_spike_outcome=\(outcome)",
                "first_scan_session_id=\(firstSession.map { String($0.value) } ?? "missing")",
                "first_matching_advertisements=\(firstCount)",
                "second_scan_session_id=\(secondSession.map { String($0.value) } ?? "not-started")",
                "second_matching_advertisements=\(secondCount)",
                "central_recovery_spike_diagnostic=central-recovery-spike.json",
                "production_gate_claim=none",
            ]
            if let failureReason {
                lines.append("central_recovery_spike_failure_reason=\(failureReason)")
            }
            return lines.joined(separator: "\n") + "\n"
        } catch {
            try? await owner.shutdown()
            throw error
        }
    } catch {
        try? backend.dispose()
        throw error
    }
}
