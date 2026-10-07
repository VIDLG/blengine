import BleHubUniFfi
import Foundation

private struct RecoveryLink {
    let connection: ConnectionId
    let session: GattSessionId
    let read: GattCharacteristicId
    let updates: GattCharacteristicId
}

private func recoveryMarker(_ directory: URL, _ marker: String) throws {
    try Data("ready\n".utf8).write(to: directory.appendingPathComponent(marker), options: .atomic)
}

private func awaitRecoveryMarker(_ directory: URL, _ marker: String) async throws {
    _ = try await awaitScanEvidence("recovery marker \(marker)", seconds: 60) {
        FileManager.default.fileExists(atPath: directory.appendingPathComponent(marker).path) ? true : nil
    }
}

private enum ExpectedRecoveryRejection {
    case connectionNotActive
    case gattNotReady
    case subscriptionNotActive
}

private func requireRecoveryRejection(
    _ expected: ExpectedRecoveryRejection,
    operation: () async throws -> Void
) async throws {
    do {
        try await operation()
    } catch let error as BlengError {
        switch (expected, error) {
        case (.connectionNotActive, .NotReady(let reason)) where reason == .connectionNotActive,
             (.gattNotReady, .NotReady(let reason)) where reason == .gattNotReady,
             (.subscriptionNotActive, .NotReady(let reason)) where reason == .subscriptionNotActive:
            return
        default:
            break
        }
    }
    throw ScanProbeFailure.invalid("stale handle operation returned an unexpected typed error")
}

private func recoveryConnect(
    owner: Bleng, evidence: ScanEvidence, peripheral: PeripheralId,
    service: BluetoothUuid, indicationOnly: Bool
) async throws -> RecoveryLink {
    let connection = try await owner.connect(peripheral: peripheral, timeoutMs: 20_000)
    _ = try await awaitScanEvidence("recovery Connecting/Connected", seconds: 5) {
        await evidence.connected(connection) ? true : nil
    }
    let session = try await owner.discoverGatt(connection: connection, timeoutMs: 25_000)
    let database = try await awaitScanEvidence("recovery database", seconds: 5) {
        await evidence.database(connection: connection, session: session)
    }
    _ = try validateFixtureDatabase(database, service: service, indicationOnly: indicationOnly)
    let characteristics = database.services.first { $0.uuid == service }?.characteristics ?? []
    guard let read = characteristics.first(where: {
        $0.uuid.value == "fff10000-0000-4000-8000-000000000000"
    }), let updates = characteristics.first(where: {
        $0.uuid.value == "fff20000-0000-4000-8000-000000000000"
    }) else { throw ScanProbeFailure.invalid("recovery fixture characteristics missing") }
    let value = try await owner.readCharacteristic(
        connection: connection, session: session, characteristic: read.id, timeoutMs: 10_000
    )
    guard value == Data([0]) else { throw ScanProbeFailure.invalid("recovery read was not 00") }
    return RecoveryLink(connection: connection, session: session, read: read.id, updates: updates.id)
}

func verifyAppleRecoveryRf(
    serviceUuid: String, notificationValueHex: String, initialValueHex: String,
    indicationOnly: Bool, runDirectory: URL, recovery: Bool
) async throws -> String {
    guard let uuid = UUID(uuidString: serviceUuid), notificationValueHex.lowercased() == "ef",
          initialValueHex == "00" else {
        throw ScanProbeFailure.invalid("recovery requires a fixture UUID, EF notification and 00 read")
    }
    let service = BluetoothUuid(value: uuid.uuidString.lowercased())
    let backend = try AppleBleBackend(queueLabel: "dev.blehub.bleng.ios-recovery-probe")
    do {
        let owner = try Bleng()
        do {
            try await owner.assembleBackend(backend: backend)
            let snapshot = try await owner.initialize(timeoutMs: 30_000)
            guard snapshot.state == .poweredOn, snapshot.scanAuthorization == .authorized,
                  snapshot.connectionAuthorization == .authorized else {
                throw ScanProbeFailure.invalid("recovery adapter not powered on and authorized")
            }
            let evidence = ScanEvidence(service: service)
            let reader = Task {
                while let event = try await owner.nextEvent(timeoutMs: 86_400_000) {
                    await evidence.record(event)
                }
            }
            do {
                let cycles = recovery ? 3 : 1
                // Device creates the directories before the host bridges provider readiness.
                if recovery {
                    for cycle in 1...cycles {
                        try FileManager.default.createDirectory(
                            at: runDirectory.appendingPathComponent("cycle-\(cycle)"),
                            withIntermediateDirectories: false
                        )
                    }
                    try recoveryMarker(runDirectory, "coordination-ready")
                    try await awaitRecoveryMarker(runDirectory.appendingPathComponent("cycle-1"), "fixture-ready")
                }
                let scan = try await owner.startScan(
                    options: ScanOptions(serviceUuids: [service], namePrefix: nil, companyId: nil, allowDuplicates: false),
                    timeoutMs: 10_000
                )
                let advertisement = try await awaitScanEvidence("recovery advertisement", seconds: 30) {
                    await evidence.advertisement(for: scan)
                }
                try await owner.stopScan(session: scan, timeoutMs: 10_000)
                _ = try await awaitScanEvidence("recovery scan stop", seconds: 5) {
                    await evidence.completedStop(for: scan) ? true : nil
                }
                var link = try await recoveryConnect(
                    owner: owner, evidence: evidence, peripheral: advertisement.peripheralId,
                    service: service, indicationOnly: indicationOnly
                )
                var connections: Set<ConnectionId> = [link.connection]
                var sessions: Set<GattSessionId> = [link.session]
                var subscriptions = Set<SubscriptionId>()
                var passiveBoundaries: [(connection: ConnectionId, valueIndex: Int, terminalIndex: Int)] = []
                var lines = [
                    "apple_bleng_corebluetooth_scan=passed",
                    "adapter_state=\(snapshot.state)",
                    "scan_authorization=\(snapshot.scanAuthorization)",
                    "connection_authorization=\(snapshot.connectionAuthorization)",
                    "service_uuid=\(serviceUuid)",
                    "scan_session_id=\(scan.value)", "peripheral_id=\(advertisement.peripheralId.value)",
                    "cycles=\(cycles)", "owner_count=1",
                ]
                for cycle in 1...cycles {
                    let directory = recovery ? runDirectory.appendingPathComponent("cycle-\(cycle)") : runDirectory
                    if recovery && cycle > 1 { try await awaitRecoveryMarker(directory, "fixture-ready") }
                    let subscription = try await owner.enableNotifications(
                        connection: link.connection, session: link.session, characteristic: link.updates,
                        mode: .platformDefault, timeoutMs: 15_000
                    )
                    guard subscriptions.insert(subscription).inserted else {
                        throw ScanProbeFailure.invalid("reused recovery subscription ID")
                    }
                    let enabled = try await awaitScanEvidence("recovery notification enable", seconds: 5) {
                        await evidence.notificationEnabled(
                            connection: link.connection, session: link.session, subscription: subscription,
                            characteristic: link.updates, mode: .notification
                        )
                    }
                    let completion = try await awaitScanEvidence("recovery enable completion", seconds: 5) {
                        await evidence.commandCompletion(enabled.command, after: enabled.index)
                    }
                    guard !completion.failed else { throw ScanProbeFailure.invalid("recovery enable failed") }
                    try recoveryMarker(directory, "subscription-enabled")
                    let value = try await awaitScanEvidence("scoped recovery EF notification", seconds: 60) {
                        await evidence.notificationValue(
                            after: completion.index, connection: link.connection, session: link.session,
                            subscription: subscription, characteristic: link.updates
                        )
                    }
                    guard value.value == Data([0xef]) else {
                        throw ScanProbeFailure.invalid("recovery notification was not EF")
                    }
                    try recoveryMarker(directory, "connected")
                    try await awaitRecoveryMarker(directory, "fault-triggered")
                    let passive: (index: Int, result: String, errorCategory: String?)
                    do {
                        passive = try await awaitScanEvidence("passive command-less connection terminal", seconds: 30) {
                            await evidence.passiveTerminal(link.connection, after: value.index)
                        }
                    } catch {
                        let observed = await evidence.connectionLifecycleDescription(link.connection)
                        throw ScanProbeFailure.invalid(
                            "\(error); connection=\(link.connection.value) value_index=\(value.index); observed lifecycle: \(observed)"
                        )
                    }
                    passiveBoundaries.append((link.connection, value.index, passive.index))
                    guard await evidence.notificationDisabled(
                        connection: link.connection, session: link.session, subscription: subscription,
                        characteristic: link.updates
                    ) == nil else {
                        throw ScanProbeFailure.invalid("subscription disabled before passive loss")
                    }
                    try recoveryMarker(directory, "passive-disconnected")
                    try await requireRecoveryRejection(.connectionNotActive) {
                        try await owner.disconnect(connection: link.connection, timeoutMs: 5_000)
                    }
                    try await requireRecoveryRejection(.connectionNotActive) {
                        _ = try await owner.readCharacteristic(
                            connection: link.connection, session: link.session, characteristic: link.read, timeoutMs: 5_000
                        )
                    }
                    try await requireRecoveryRejection(.connectionNotActive) {
                        try await owner.disableNotifications(
                            connection: link.connection, session: link.session, subscription: subscription, timeoutMs: 5_000
                        )
                    }
                    try await awaitRecoveryMarker(directory, "recovery-ready")
                    let recovered = try await recoveryConnect(
                        owner: owner, evidence: evidence, peripheral: advertisement.peripheralId,
                        service: service, indicationOnly: indicationOnly
                    )
                    guard connections.insert(recovered.connection).inserted,
                          sessions.insert(recovered.session).inserted else {
                        throw ScanProbeFailure.invalid("reused recovery connection/GATT ID")
                    }
                    // Also reject old GATT/subscription handles against the new live connection.
                    try await requireRecoveryRejection(.gattNotReady) {
                        _ = try await owner.readCharacteristic(
                            connection: recovered.connection, session: link.session, characteristic: link.read, timeoutMs: 5_000
                        )
                    }
                    try await requireRecoveryRejection(.subscriptionNotActive) {
                        try await owner.disableNotifications(
                            connection: recovered.connection, session: recovered.session, subscription: subscription, timeoutMs: 5_000
                        )
                    }
                    try await Task.sleep(nanoseconds: 2_000_000_000)
                    guard !(await evidence.hasLateValueRoutes()), !(await evidence.hasPostStopAdvertisement()) else {
                        throw ScanProbeFailure.invalid("late recovery value/scan route")
                    }
                    let prefix = "cycle_\(cycle)_"
                    lines.append(contentsOf: [
                        "\(prefix)connection_id=\(link.connection.value)",
                        "\(prefix)gatt_session_id=\(link.session.value)",
                        "\(prefix)subscription_id=\(subscription.value)",
                        "\(prefix)characteristic_id=\(link.updates.value)",
                        "\(prefix)actual_mode=notification", "\(prefix)notification_value_hex=ef",
                        "\(prefix)enable_index=\(enabled.index)", "\(prefix)enable_terminal_index=\(completion.index)",
                        "\(prefix)value_index=\(value.index)", "\(prefix)disconnect_index=\(passive.index)",
                        "\(prefix)passive_result=\(passive.result)",
                        "\(prefix)connection_state_sequence=connecting,connected,\(passive.result)",
                        "\(prefix)passive_command=absent",
                        "\(prefix)active_subscription_at_disconnect=passed",
                        "\(prefix)stale_connection=ConnectionNotActive",
                        "\(prefix)stale_gatt=ConnectionNotActive",
                        "\(prefix)stale_subscription=ConnectionNotActive",
                        "\(prefix)stale_gatt_on_reconnect=GattNotReady",
                        "\(prefix)stale_subscription_on_reconnect=SubscriptionNotActive",
                        "\(prefix)post_disconnect_silence=passed",
                        "\(prefix)initial_read_hex=00", "\(prefix)recovered_read_hex=00",
                        "\(prefix)recovered_connection_id=\(recovered.connection.value)",
                        "\(prefix)recovered_gatt_session_id=\(recovered.session.value)",
                        "\(prefix)gatt_characteristic_count=\(indicationOnly ? 5 : 4)",
                        "\(prefix)gatt_cccd_count=\(indicationOnly ? 2 : 1)",
                        "\(prefix)status=passed",
                    ])
                    if let category = passive.errorCategory {
                        lines.append("\(prefix)passive_error_category=\(category)")
                    }
                    try recoveryMarker(directory, "cycle-passed")
                    // recovery-ios does not wait for an active disconnect between cycles.
                    link = recovered
                }
                try await owner.disconnect(connection: link.connection, timeoutMs: 20_000)
                _ = try await awaitScanEvidence("final active disconnect", seconds: 5) {
                    await evidence.activelyDisconnected(link.connection) ? true : nil
                }
                try await Task.sleep(nanoseconds: 2_000_000_000)
                try await owner.shutdown()
                try await reader.value
                for boundary in passiveBoundaries {
                    guard await evidence.passiveTerminal(boundary.connection, after: boundary.valueIndex)?.index
                        == boundary.terminalIndex else {
                        let observed = await evidence.connectionLifecycleDescription(boundary.connection)
                        throw ScanProbeFailure.invalid("passive terminal was not unique; observed lifecycle: \(observed)")
                    }
                }
                guard !(await evidence.hasLateValueRoutes()) else {
                    throw ScanProbeFailure.invalid("late value route after recovery shutdown")
                }
                lines.append("final_disconnect=passed")
                lines.append("apple_bleng_corebluetooth_\(recovery ? "recovery" : "passive-disconnect")=passed")
                return lines.joined(separator: "\n") + "\n"
            } catch {
                try? await owner.shutdown()
                _ = try? await reader.value
                throw error
            }
        } catch {
            try? await owner.shutdown()
            throw error
        }
    } catch {
        try? backend.dispose()
        throw error
    }
}
