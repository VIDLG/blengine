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

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

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
        func argument(_ name: String) -> String? {
            guard let position = arguments.firstIndex(of: name),
                  arguments.indices.contains(position + 1) else { return nil }
            return arguments[position + 1]
        }
        let notificationValueHex = argument("--rf-notification-value-hex")
        if (subscriptionRequested || faultRequested) && (scanService == nil || notificationValueHex == nil) {
            return false
        }
        let discover = arguments.contains("--rf-gatt") || ioRequested || subscriptionRequested
        let connect = arguments.contains("--rf-connect") || discover || rssiRequested
        if connect && scanService == nil { return false }
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
        if subscriptionRequested || faultRequested {
            do {
                notificationRunDirectory = try prepareBlengRfRun(runId: runId)
            } catch {
                return false
            }
        }

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
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = controller
        window.makeKeyAndVisible()
        self.window = window

        Task {
            let result: String
            do {
                if faultRequested {
                    guard let scanService, let notificationValueHex, let notificationRunDirectory,
                          let initial = argument("--rf-initial-hex") else {
                        throw ScanProbeFailure.invalid("recovery RF arguments are incomplete")
                    }
                    status.text = "BLeng + CoreBluetooth: \(faultScenario) RF"
                    result = try await verifyAppleRecoveryRf(
                        serviceUuid: scanService, notificationValueHex: notificationValueHex,
                        initialValueHex: initial, indicationOnly: indicationOnly,
                        runDirectory: notificationRunDirectory, recovery: recoveryRequested
                    )
                    status.text = "BLeng + CoreBluetooth: RF \(faultScenario) passed"
                } else if subscriptionRequested {
                    guard let scanService, let notificationValueHex, let notificationRunDirectory else {
                        throw ScanProbeFailure.invalid("notification RF arguments are incomplete")
                    }
                    status.text = "BLeng + CoreBluetooth: \(subscriptionScenario) RF"
                    let evidence = try await verifyAppleSubscriptionRf(
                        serviceUuid: scanService, notificationValueHex: notificationValueHex,
                        indicationOnly: indicationOnly, runDirectory: notificationRunDirectory,
                        strictIndication: indicationRequested
                    )
                    status.text = "BLeng + CoreBluetooth: RF \(subscriptionScenario) passed"
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
                        "gatt_characteristic_count=\(indicationOnly ? 5 : 4)",
                        "gatt_cccd_count=\(evidence.cccdCount)",
                        "\(subscriptionScenario)_value_hex=\(evidence.valueHex)",
                        "actual_mode=\(evidence.actualMode == .indication ? "indication" : "notification")",
                        "subscription_id=\(evidence.subscription.value)",
                        "\(subscriptionScenario)_enable_disable_order=passed",
                        "\(subscriptionScenario)_post_disable_silence=passed",
                    ]
                    if indicationRequested {
                        lines.append(contentsOf: [
                            "strict_dual_indication_rejected=passed",
                            "strict_dual_no_active_route=passed",
                        ])
                    }
                    lines.append("apple_bleng_corebluetooth_\(subscriptionScenario)=passed")
                    result = lines.joined(separator: "\n") + "\n"
                } else if let scanService {
                    status.text = "BLeng + CoreBluetooth: scanning"
                    let evidence = try await verifyAppleRf(
                        serviceUuid: scanService, connect: connect,
                        discover: discover, indicationOnly: indicationOnly,
                        ioInputs: ioInputs, readRssi: rssiRequested
                    )
                    status.text = rssiRequested ? "BLeng + CoreBluetooth: RF RSSI passed" :
                        (ioRequested ? "BLeng + CoreBluetooth: RF I/O passed" :
                         (discover ? "BLeng + CoreBluetooth: RF GATT passed" :
                          (connect ? "BLeng + CoreBluetooth: RF connection passed" :
                           "BLeng + CoreBluetooth: RF scan passed")))
                    var lines = [
                        "apple_bleng_corebluetooth_scan=passed",
                        "adapter_state=\(evidence.snapshot.state)",
                        "scan_authorization=\(evidence.snapshot.scanAuthorization)",
                        "service_uuid=\(scanService)",
                        "scan_session_id=\(evidence.advertisement.scanSessionId.value)",
                        "peripheral_id=\(evidence.advertisement.peripheralId.value)",
                    ]
                    if connect, let connection = evidence.connection {
                        lines.append(contentsOf: [
                            "apple_bleng_corebluetooth_connection=passed",
                            "connection_id=\(connection.value)",
                            "connection_state_sequence=connecting,connected,disconnected",
                        ])
                    }
                    if discover, let gatt = evidence.gatt, let cccdCount = evidence.cccdCount {
                        lines.append(contentsOf: [
                            "apple_bleng_corebluetooth_gatt=passed",
                            "gatt_session_id=\(gatt.value)",
                            "gatt_characteristic_count=\(indicationOnly ? 5 : 4)",
                            "gatt_cccd_count=\(cccdCount)",
                        ])
                    }
                    if ioRequested, let io = evidence.io {
                        lines.append(contentsOf: [
                            "apple_bleng_corebluetooth_io=passed",
                            "initial_read_hex=\(io.initial)",
                            "with_response_readback_hex=\(io.withResponse)",
                            "without_response_delayed_readback_hex=\(io.withoutResponse)",
                        ])
                    }
                    if rssiRequested, let rssi = evidence.rssi {
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
                if faultRequested {
                    result = "apple_bleng_corebluetooth_\(faultScenario)=failed\nerror=\(error)\n"
                } else if subscriptionRequested {
                    result = "apple_bleng_corebluetooth_\(subscriptionScenario)=failed\nerror=\(error)\n"
                } else {
                    let scenario = scanService == nil ? "initialize" :
                        (rssiRequested ? "rssi" :
                         (ioRequested ? "io" : (discover ? "gatt" : (connect ? "connection" : "scan"))))
                    result = "apple_bleng_corebluetooth_\(scenario)=failed\nerror=\(error)\n"
                }
            }
            let resultWithRunId = "apple_bleng_corebluetooth_run_id=\(runId)\n" + result
            if let notificationRunDirectory {
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
        return true
    }
}
