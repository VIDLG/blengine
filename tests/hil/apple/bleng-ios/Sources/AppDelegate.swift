import BleHubUniFfi
import Foundation
import UIKit

// The initialization-only path never starts RF; scan is an explicit opt-in HIL mode.
private func verifyAppleInitialization() async throws -> AdapterSnapshot {
    let backend = try AppleBleBackend(queueLabel: "dev.blehub.bleng.ios-init-probe")
    do {
        let owner = try Bleng()
        do {
            try await owner.assembleBackend(backend: backend)
            let snapshot = try await owner.initialize(timeoutMs: 30_000)
            try await owner.shutdown()
            return snapshot
        } catch {
            try? await owner.shutdown()
            throw error
        }
    } catch {
        try? backend.dispose()
        throw error
    }
}

private struct AppLaunchConfiguration {
    let runId: String
    let systemLossArguments: SystemLossArguments?
    let freshOwnerLifecycleRequested: Bool
    let centralRecoverySpikeRequested: Bool
    let scanService: String?
    let ioRequested: Bool
    let rssiRequested: Bool
    let notificationRequested: Bool
    let indicationRequested: Bool
    let faultRequested: Bool
    let faultScenario: String
    let subscriptionRequested: Bool
    let subscriptionScenario: String
    let notificationValueHex: String?
    let initialValueHex: String?
    let indicationOnly: Bool
    let connect: Bool
    let discover: Bool
    let ioInputs: RfIoInputs?
    let notificationRunDirectory: URL?
}

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    private var launchConfiguration: AppLaunchConfiguration?
    private var didStartProbe = false

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: "--run-id"),
              arguments.indices.contains(index + 1),
              UUID(uuidString: arguments[index + 1]) != nil
        else { return false }
        let runId = arguments[index + 1]
        let systemLossArguments: SystemLossArguments?
        do {
            systemLossArguments = try SystemLossArguments.parse(arguments)
        } catch {
            return false
        }
        let freshOwnerLifecycleRequested = arguments.contains("--rf-fresh-owner-lifecycle-spike")
        let centralRecoverySpikeRequested = arguments.contains("--rf-central-recovery-spike")
        let scanIndex = arguments.firstIndex(of: "--rf-scan-service-uuid")
        let scanService = scanIndex.flatMap { arguments.indices.contains($0 + 1) ? arguments[$0 + 1] : nil }
        if scanIndex != nil && scanService == nil { return false }
        let ioRequested = arguments.contains("--rf-io")
        let rssiRequested = arguments.contains("--rf-rssi")
        let notificationRequested = arguments.contains("--rf-notification")
        let indicationRequested = arguments.contains("--rf-indication")
        let passiveRequested = arguments.contains("--rf-passive-disconnect")
        let recoveryRequested = arguments.contains("--rf-recovery")
        let faultRequested = passiveRequested || recoveryRequested
        let faultScenario = recoveryRequested ? "recovery" : "passive-disconnect"
        if [notificationRequested, indicationRequested, passiveRequested, recoveryRequested].filter({ $0 }).count > 1 {
            return false
        }
        let subscriptionRequested = notificationRequested || indicationRequested
        let subscriptionScenario = indicationRequested ? "indication" : "notification"
        if freshOwnerLifecycleRequested &&
            (systemLossArguments != nil || centralRecoverySpikeRequested || ioRequested ||
             rssiRequested || notificationRequested || indicationRequested ||
             passiveRequested || recoveryRequested || scanService != nil) {
            return false
        }
        if centralRecoverySpikeRequested &&
            (systemLossArguments != nil || freshOwnerLifecycleRequested || ioRequested ||
             rssiRequested || notificationRequested || indicationRequested || passiveRequested ||
             recoveryRequested || scanService == nil) {
            return false
        }
        func argument(_ name: String) -> String? {
            guard let position = arguments.firstIndex(of: name),
                  arguments.indices.contains(position + 1) else { return nil }
            return arguments[position + 1]
        }
        let notificationValueHex = argument("--rf-notification-value-hex")
        let initialValueHex = argument("--rf-initial-hex")
        if (subscriptionRequested || faultRequested) &&
            (scanService == nil || notificationValueHex == nil || initialValueHex == nil) {
            return false
        }
        let discover = arguments.contains("--rf-gatt") || ioRequested || subscriptionRequested
        let connect = arguments.contains("--rf-connect") || discover || rssiRequested
        if connect && scanService == nil { return false }
        if centralRecoverySpikeRequested && (connect || discover) { return false }
        let indicationOnly = arguments.contains("--rf-indication-only")
        if indicationRequested && !indicationOnly { return false }
        var ioInputs: RfIoInputs?
        if ioRequested {
            guard let initial = argument("--rf-initial-hex"),
                  let response = argument("--rf-write-response-hex"),
                  let noResponse = argument("--rf-write-no-response-hex") else { return false }
            ioInputs = RfIoInputs(
                initial: initial, withResponse: response, withoutResponse: noResponse
            )
        }
        var notificationRunDirectory: URL?
        if subscriptionRequested || faultRequested || systemLossArguments != nil ||
            centralRecoverySpikeRequested || freshOwnerLifecycleRequested {
            do {
                notificationRunDirectory = try prepareBlengRfRun(runId: runId)
            } catch {
                return false
            }
        }
        if systemLossArguments != nil, let notificationRunDirectory {
            let launchDocument: [String: Any] = [
                "schema_version": 1,
                "run_id": runId,
                "state": "app-launch-accepted",
                "system_loss_arguments": true,
            ]
            if let data = try? JSONSerialization.data(
                withJSONObject: launchDocument,
                options: [.sortedKeys, .withoutEscapingSlashes]
            ) {
                try? data.write(
                    to: notificationRunDirectory.appendingPathComponent("launch-receipt.json"),
                    options: .atomic
                )
            }
        }
        launchConfiguration = AppLaunchConfiguration(
            runId: runId,
            systemLossArguments: systemLossArguments,
            freshOwnerLifecycleRequested: freshOwnerLifecycleRequested,
            centralRecoverySpikeRequested: centralRecoverySpikeRequested,
            scanService: scanService,
            ioRequested: ioRequested,
            rssiRequested: rssiRequested,
            notificationRequested: notificationRequested,
            indicationRequested: indicationRequested,
            faultRequested: faultRequested,
            faultScenario: faultScenario,
            subscriptionRequested: subscriptionRequested,
            subscriptionScenario: subscriptionScenario,
            notificationValueHex: notificationValueHex,
            initialValueHex: initialValueHex,
            indicationOnly: indicationOnly,
            connect: connect,
            discover: discover,
            ioInputs: ioInputs,
            notificationRunDirectory: notificationRunDirectory
        )
        return true
    }

    func application(
        _ application: UIApplication,
        configurationForConnecting connectingSceneSession: UISceneSession,
        options: UIScene.ConnectionOptions
    ) -> UISceneConfiguration {
        UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
    }

    private func writeSystemLossReceipt(_ name: String, state: String) {
        guard let configuration = launchConfiguration,
              configuration.systemLossArguments != nil,
              let directory = configuration.notificationRunDirectory else { return }
        let document: [String: Any] = [
            "schema_version": 1,
            "run_id": configuration.runId,
            "state": state,
        ]
        guard let data = try? JSONSerialization.data(
            withJSONObject: document,
            options: [.sortedKeys, .withoutEscapingSlashes]
        ) else { return }
        try? data.write(to: directory.appendingPathComponent(name), options: .atomic)
    }

    /// SceneDelegate owns the scene window; this method keeps all existing probe work in this AppDelegate.
    func connect(window: UIWindow) {
        guard !didStartProbe, let configuration = launchConfiguration else { return }
        didStartProbe = true
        writeSystemLossReceipt("scene-receipt.json", state: "scene-connected")
        self.window = window

        let status = UILabel()
        status.textAlignment = .center
        status.numberOfLines = 0
        status.text = "BLeng + CoreBluetooth: initializing"
        let controller = UIViewController()
        controller.view.backgroundColor = .systemBackground
        controller.view.addSubview(status)
        status.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            status.centerXAnchor.constraint(equalTo: controller.view.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: controller.view.centerYAnchor),
        ])
        window.rootViewController = controller
        window.makeKeyAndVisible()

        let runId = configuration.runId
        writeSystemLossReceipt("task-receipt.json", state: "task-started")
        Task {
            let result: String
            do {
                if configuration.freshOwnerLifecycleRequested {
                    guard let notificationRunDirectory = configuration.notificationRunDirectory else {
                        throw ScanProbeFailure.invalid("fresh-owner lifecycle run directory required")
                    }
                    status.text = "BLeng: fresh-owner lifecycle"
                    result = try await verifyAppleFreshOwnerLifecycle(
                        runId: runId,
                        runDirectory: notificationRunDirectory,
                        status: { value in
                            DispatchQueue.main.async { status.text = value }
                        }
                    )
                    status.text = "BLeng: fresh-owner lifecycle finished"
                } else if configuration.centralRecoverySpikeRequested {
                    guard let scanService = configuration.scanService,
                          let notificationRunDirectory = configuration.notificationRunDirectory else {
                        throw ScanProbeFailure.invalid("central recovery spike arguments are incomplete")
                    }
                    status.text = "BLeng: central recovery spike"
                    result = try await verifyAppleCentralRecoverySpike(
                        serviceUuid: scanService,
                        runId: runId,
                        runDirectory: notificationRunDirectory,
                        status: { value in
                            DispatchQueue.main.async { status.text = value }
                        }
                    )
                    status.text = "BLeng: central recovery spike finished"
                } else if let systemLossArguments = configuration.systemLossArguments {
                    guard let notificationRunDirectory = configuration.notificationRunDirectory else {
                        throw SystemLossFailure.invalid("system-loss run directory required")
                    }
                    status.text = "BLeng: system-loss \(systemLossArguments.active ? "active" : "witness")"
                    writeSystemLossReceipt("entered-receipt.json", state: "system-loss-entered")
                    result = await verifyAppleSystemLossWitness(
                        arguments: systemLossArguments,
                        runDirectory: notificationRunDirectory,
                        status: { value in
                            DispatchQueue.main.async { status.text = value }
                        }
                    )
                    status.text = "BLeng: system-loss finished; gate pending"
                } else if configuration.faultRequested {
                    guard let scanService = configuration.scanService,
                          let notificationValueHex = configuration.notificationValueHex,
                          let notificationRunDirectory = configuration.notificationRunDirectory,
                          let initial = configuration.initialValueHex else {
                        throw ScanProbeFailure.invalid("recovery RF arguments are incomplete")
                    }
                    status.text = "BLeng + CoreBluetooth: \(configuration.faultScenario) RF"
                    result = try await verifyAppleRecoveryRf(
                        serviceUuid: scanService, notificationValueHex: notificationValueHex,
                        initialValueHex: initial, indicationOnly: configuration.indicationOnly,
                        runDirectory: notificationRunDirectory,
                        recovery: configuration.faultScenario == "recovery"
                    )
                    status.text = "BLeng + CoreBluetooth: RF \(configuration.faultScenario) passed"
                } else if configuration.subscriptionRequested {
                    guard let scanService = configuration.scanService,
                          let notificationValueHex = configuration.notificationValueHex,
                          let notificationRunDirectory = configuration.notificationRunDirectory else {
                        throw ScanProbeFailure.invalid("notification RF arguments are incomplete")
                    }
                    status.text = "BLeng + CoreBluetooth: \(configuration.subscriptionScenario) RF"
                    let evidence = try await verifyAppleSubscriptionRf(
                        serviceUuid: scanService, notificationValueHex: notificationValueHex,
                        indicationOnly: configuration.indicationOnly,
                        runDirectory: notificationRunDirectory,
                        strictIndication: configuration.indicationRequested
                    )
                    status.text = "BLeng + CoreBluetooth: RF \(configuration.subscriptionScenario) passed"
                    var lines = [
                        "apple_bleng_corebluetooth_scan=passed",
                        "adapter_state=\(evidence.snapshot.state)",
                        "scan_authorization=\(evidence.snapshot.scanAuthorization)",
                        "service_uuid=\(scanService)",
                        "scan_session_id=\(evidence.advertisement.scanSessionId.value)",
                        "peripheral_id=\(evidence.advertisement.peripheralId.value)",
                        "apple_bleng_corebluetooth_connection=passed",
                        "connection_id=\(evidence.connection.value)",
                        "connection_state_sequence=connecting,connected,disconnected",
                        "apple_bleng_corebluetooth_gatt=passed",
                        "gatt_session_id=\(evidence.gatt.value)",
                        "gatt_characteristic_count=\(configuration.indicationOnly ? 5 : 4)",
                        "gatt_cccd_count=\(evidence.cccdCount)",
                        "\(configuration.subscriptionScenario)_value_hex=\(evidence.valueHex)",
                        "actual_mode=\(evidence.actualMode == .indication ? "indication" : "notification")",
                        "subscription_id=\(evidence.subscription.value)",
                        "\(configuration.subscriptionScenario)_enable_disable_order=passed",
                        "\(configuration.subscriptionScenario)_post_disable_silence=passed",
                    ]
                    if configuration.indicationRequested {
                        lines.append(contentsOf: [
                            "strict_dual_indication_rejected=passed",
                            "strict_dual_no_active_route=passed",
                        ])
                    }
                    lines.append("apple_bleng_corebluetooth_\(configuration.subscriptionScenario)=passed")
                    result = lines.joined(separator: "\n") + "\n"
                } else if let scanService = configuration.scanService {
                    status.text = "BLeng + CoreBluetooth: scanning"
                    let evidence = try await verifyAppleRf(
                        serviceUuid: scanService, connect: configuration.connect,
                        discover: configuration.discover, indicationOnly: configuration.indicationOnly,
                        ioInputs: configuration.ioInputs, readRssi: configuration.rssiRequested
                    )
                    status.text = configuration.rssiRequested ? "BLeng + CoreBluetooth: RF RSSI passed" :
                        (configuration.ioRequested ? "BLeng + CoreBluetooth: RF I/O passed" :
                         (configuration.discover ? "BLeng + CoreBluetooth: RF GATT passed" :
                          (configuration.connect ? "BLeng + CoreBluetooth: RF connection passed" :
                           "BLeng + CoreBluetooth: RF scan passed")))
                    var lines = [
                        "apple_bleng_corebluetooth_scan=passed",
                        "adapter_state=\(evidence.snapshot.state)",
                        "scan_authorization=\(evidence.snapshot.scanAuthorization)",
                        "service_uuid=\(scanService)",
                        "scan_session_id=\(evidence.advertisement.scanSessionId.value)",
                        "peripheral_id=\(evidence.advertisement.peripheralId.value)",
                    ]
                    if configuration.connect, let connection = evidence.connection {
                        lines.append(contentsOf: [
                            "apple_bleng_corebluetooth_connection=passed",
                            "connection_id=\(connection.value)",
                            "connection_state_sequence=connecting,connected,disconnected",
                        ])
                    }
                    if configuration.discover, let gatt = evidence.gatt, let cccdCount = evidence.cccdCount {
                        lines.append(contentsOf: [
                            "apple_bleng_corebluetooth_gatt=passed",
                            "gatt_session_id=\(gatt.value)",
                            "gatt_characteristic_count=\(configuration.indicationOnly ? 5 : 4)",
                            "gatt_cccd_count=\(cccdCount)",
                        ])
                    }
                    if configuration.ioRequested, let io = evidence.io {
                        lines.append(contentsOf: [
                            "apple_bleng_corebluetooth_io=passed",
                            "initial_read_hex=\(io.initial)",
                            "with_response_readback_hex=\(io.withResponse)",
                            "without_response_delayed_readback_hex=\(io.withoutResponse)",
                        ])
                    }
                    if configuration.rssiRequested, let rssi = evidence.rssi {
                        lines.append(contentsOf: [
                            "apple_bleng_corebluetooth_rssi=passed",
                            "rssi_dbm=\(rssi)",
                        ])
                    }
                    result = lines.joined(separator: "\n") + "\n"
                } else {
                    let snapshot = try await verifyAppleInitialization()
                    status.text = "BLeng + CoreBluetooth: initialized (\(snapshot.state))"
                    result = [
                        "apple_bleng_corebluetooth_initialize=passed",
                        "adapter_state=\(snapshot.state)",
                        "scan_authorization=\(snapshot.scanAuthorization)",
                        "connection_authorization=\(snapshot.connectionAuthorization)",
                    ].joined(separator: "\n") + "\n"
                }
            } catch {
                status.text = "BLeng + CoreBluetooth: failed"
                if configuration.systemLossArguments != nil,
                   let notificationRunDirectory = configuration.notificationRunDirectory {
                    let failureDocument: [String: Any] = [
                        "schema_version": 1,
                        "run_id": runId,
                        "state": "app-failed",
                        "error": String(describing: error),
                    ]
                    if let data = try? JSONSerialization.data(
                        withJSONObject: failureDocument,
                        options: [.sortedKeys, .withoutEscapingSlashes]
                    ) {
                        try? data.write(
                            to: notificationRunDirectory.appendingPathComponent("failure-receipt.json"),
                            options: .atomic
                        )
                    }
                }
                if configuration.freshOwnerLifecycleRequested {
                    result = "apple_bleng_fresh_owner_lifecycle=failed\nproduction_gate_claim=none\nerror=\(error)\n"
                } else if configuration.centralRecoverySpikeRequested {
                    result = "apple_bleng_corebluetooth_central_recovery_spike=failed\nproduction_gate_claim=none\nerror=\(error)\n"
                } else if configuration.systemLossArguments != nil {
                    result = "apple_bleng_system_loss_witness=failed\ngate=pending\nrf=not-evaluated\nerror=\(error)\n"
                } else if configuration.faultRequested {
                    result = "apple_bleng_corebluetooth_\(configuration.faultScenario)=failed\nerror=\(error)\n"
                } else if configuration.subscriptionRequested {
                    result = "apple_bleng_corebluetooth_\(configuration.subscriptionScenario)=failed\nerror=\(error)\n"
                } else {
                    let scenario = configuration.scanService == nil ? "initialize" :
                        (configuration.rssiRequested ? "rssi" :
                         (configuration.ioRequested ? "io" :
                          (configuration.discover ? "gatt" :
                           (configuration.connect ? "connection" : "scan"))))
                    result = "apple_bleng_corebluetooth_\(scenario)=failed\nerror=\(error)\n"
                }
            }
            let resultWithRunId = "apple_bleng_corebluetooth_run_id=\(runId)\n" + result
            if let notificationRunDirectory = configuration.notificationRunDirectory {
                do {
                    try resultWithRunId.write(
                        to: notificationRunDirectory.appendingPathComponent("result.txt"),
                        atomically: true, encoding: .utf8
                    )
                } catch {
                    NSLog("cannot write BLeng CoreBluetooth RF result: %@", String(describing: error))
                }
            }
            let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            let output = documents.appendingPathComponent("bleng-corebluetooth-init-result.txt")
            do {
                try resultWithRunId.write(to: output, atomically: true, encoding: .utf8)
            } catch {
                NSLog("cannot write BLeng CoreBluetooth result: %@", String(describing: error))
            }
        }
    }
}
