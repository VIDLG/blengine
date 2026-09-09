import BleHubUniFfi
import Foundation
import UIKit

private enum IOSProbeError: Error, CustomStringConvertible {
    case initializationTimeout
    case authorizationTimeout
    case authorizationRejected(AuthorizationState)
    case adapterUnavailable(AdapterSnapshot)
    case unexpectedInitializationEvents([BleEvent])
    case invalidDiagnosticTargetIdentifier(String)
    case invalidDiagnosticScanDuration(String)

    var description: String {
        switch self {
        case .initializationTimeout:
            "timed out waiting for CoreBluetooth initialization events"
        case .authorizationTimeout:
            "timed out waiting for iOS Bluetooth authorization"
        case .authorizationRejected(let authorization):
            "iOS Bluetooth authorization became \(authorization)"
        case .adapterUnavailable(let snapshot):
            "iOS CoreBluetooth adapter is unavailable with snapshot \(snapshot)"
        case .unexpectedInitializationEvents(let events):
            "unexpected initialization event sequence: \(events)"
        case .invalidDiagnosticTargetIdentifier(let value):
            "invalid CoreBluetooth diagnostic target identifier: \(value)"
        case .invalidDiagnosticScanDuration(let value):
            "invalid CoreBluetooth diagnostic scan duration: \(value)"
        }
    }
}

private func argument(after flag: String) -> String? {
    guard let index = CommandLine.arguments.firstIndex(of: flag),
          CommandLine.arguments.indices.contains(index + 1)
    else {
        return nil
    }
    return CommandLine.arguments[index + 1]
}

private func latestIOSAdapterSnapshot(in events: [BleEvent]) -> AdapterSnapshot? {
    for event in events.reversed() {
        if case .adapterSnapshotChanged(_, let snapshot) = event {
            return snapshot
        }
    }
    return nil
}

private final class ProbeRunner {
    private let runId: String

    init(runId: String) {
        self.runId = runId
    }

    func run() -> String {
        do {
            let backend = try AppleBleBackend(
                queueLabel: "dev.blehub.apple.ios-device-probe"
            )
            let fixture = try SwiftProbeHarness(backend: backend)
            defer {
                try? fixture.dispose()
            }

            let commandId = CommandId(value: 1)
            try fixture.initialize(command: InitializeCommand(commandId: commandId))

            let initializationDeadline = Date().addingTimeInterval(120)
            var initialSnapshot: AdapterSnapshot?
            while Date() < initializationDeadline {
                let events = fixture.snapshot().events
                if events.count >= 2 {
                    guard case .adapterSnapshotChanged(let observedCommandId, let snapshot) = events[0],
                          observedCommandId == commandId,
                          case .commandCompleted(let completedCommandId, let error) = events[1],
                          completedCommandId == commandId,
                          error == nil
                    else {
                        throw IOSProbeError.unexpectedInitializationEvents(events)
                    }
                    initialSnapshot = snapshot
                    break
                }
                Thread.sleep(forTimeInterval: 0.05)
            }
            guard let initialSnapshot else {
                throw IOSProbeError.initializationTimeout
            }

            var output = [
                "apple_ios_probe_run_id=\(runId)",
                "apple_ios_corebluetooth_adapter_state=\(initialSnapshot.state)",
                "apple_ios_corebluetooth_scan_authorization=\(initialSnapshot.scanAuthorization)",
                "apple_ios_corebluetooth_connection_authorization=\(initialSnapshot.connectionAuthorization)",
                "apple_ios_corebluetooth_initialize=passed",
            ]

            let authorizationDeadline = Date().addingTimeInterval(120)
            var authorizedSnapshot: AdapterSnapshot?
            while Date() < authorizationDeadline {
                if let snapshot = latestIOSAdapterSnapshot(in: fixture.snapshot().events) {
                    switch snapshot.scanAuthorization {
                    case .authorized:
                        authorizedSnapshot = snapshot
                    case .denied, .restricted:
                        throw IOSProbeError.authorizationRejected(snapshot.scanAuthorization)
                    case .notDetermined, .notRequired:
                        break
                    }
                }
                if authorizedSnapshot != nil {
                    break
                }
                Thread.sleep(forTimeInterval: 0.05)
            }
            guard let authorizedSnapshot else {
                throw IOSProbeError.authorizationTimeout
            }
            guard authorizedSnapshot.state == .poweredOn else {
                throw IOSProbeError.adapterUnavailable(authorizedSnapshot)
            }

            output.append(
                "apple_ios_corebluetooth_authorized_adapter_state=\(authorizedSnapshot.state)"
            )
            output.append("apple_ios_corebluetooth_authorization=passed")
            output.append("apple_ios_corebluetooth_runtime=passed")

            if let serviceUuid = argument(after: "--rf-service-uuid") {
                let targetIdentifierValue = argument(after: "--rf-target-identifier")
                let targetIdentifier: UUID?
                if let targetIdentifierValue {
                    guard let parsed = UUID(uuidString: targetIdentifierValue) else {
                        throw IOSProbeError.invalidDiagnosticTargetIdentifier(
                            targetIdentifierValue
                        )
                    }
                    targetIdentifier = parsed
                } else {
                    targetIdentifier = nil
                }
                let unfiltered = CommandLine.arguments.contains("--rf-unfiltered") ||
                    targetIdentifier != nil
                let allowDuplicates = !CommandLine.arguments.contains("--rf-no-duplicates")
                let scanDurationValue = argument(after: "--rf-scan-seconds")
                let scanDuration: TimeInterval
                if let scanDurationValue {
                    guard let parsed = TimeInterval(scanDurationValue), parsed > 0 else {
                        throw IOSProbeError.invalidDiagnosticScanDuration(scanDurationValue)
                    }
                    scanDuration = parsed
                } else {
                    scanDuration = 30
                }
                let diagnosticTargetPeripheralId: (() -> PeripheralId?)?
                if let targetIdentifier {
                    diagnosticTargetPeripheralId = {
                        backend.diagnosticPeripheralId(for: targetIdentifier)
                    }
                } else {
                    diagnosticTargetPeripheralId = nil
                }
                AppleAdvertisementDiagnostics.reset()
                let scan = try runScan(
                    fixture: fixture,
                    serviceUuid: serviceUuid,
                    unfiltered: unfiltered,
                    allowDuplicates: allowDuplicates,
                    diagnosticTargetPeripheralId: diagnosticTargetPeripheralId,
                    scanDuration: scanDuration
                )
                output.append(contentsOf: scan.output.map {
                    $0.replacingOccurrences(of: "apple_macos_", with: "apple_ios_")
                })
                output.append(contentsOf: AppleAdvertisementDiagnostics.snapshot().map {
                    "apple_ios_corebluetooth_raw_advertisement=\($0)"
                })
                if unfiltered && targetIdentifier == nil {
                    output.append("apple_ios_corebluetooth_unfiltered_scan=passed")
                    return output.joined(separator: "\n") + "\n"
                }
                if let targetIdentifier {
                    output.append(
                        "apple_ios_corebluetooth_diagnostic_target_identifier=\(targetIdentifier.uuidString.lowercased())"
                    )
                    output.append("apple_ios_corebluetooth_diagnostic_target=matched")
                }
                output.append(contentsOf: try runConnection(
                    fixture: fixture,
                    peripheralId: scan.advertisement.peripheralId,
                    discoverGattServiceUuid: serviceUuid,
                    runGattIoAfterDiscovery: true,
                    runNotificationsAfterDiscovery: false,
                    fixtureCoordinationPath: nil
                ).map {
                    $0.replacingOccurrences(of: "apple_macos_", with: "apple_ios_")
                })
                output.append("apple_ios_corebluetooth_rf=passed")
            }
            return output.joined(separator: "\n") + "\n"
        } catch {
            let diagnostics = AppleAdvertisementDiagnostics.snapshot().map {
                "apple_ios_corebluetooth_raw_advertisement=\($0)"
            }.joined(separator: "\n")
            let suffix = diagnostics.isEmpty ? "" : "\n\(diagnostics)"
            return "apple_ios_probe_run_id=\(runId)\napple_ios_corebluetooth_probe=failed: \(error)\(suffix)\n"
        }
    }
}

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    private var statusLabel: UILabel?
    private var runner: ProbeRunner?
    private var lifecycleEvents: [String] = []

    private func recordLifecycle(_ event: String) {
        lifecycleEvents.append("apple_ios_application_lifecycle=\(event)")
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        recordLifecycle("didBecomeActive")
    }

    func applicationWillResignActive(_ application: UIApplication) {
        recordLifecycle("willResignActive")
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        recordLifecycle("didEnterBackground")
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        recordLifecycle("willEnterForeground")
    }

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        let viewController = UIViewController()
        viewController.view.backgroundColor = .systemBackground

        let label = UILabel()
        label.translatesAutoresizingMaskIntoConstraints = false
        label.numberOfLines = 0
        label.textAlignment = .center
        label.text = "BleHub iOS CoreBluetooth probe is running…"
        viewController.view.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: viewController.view.leadingAnchor, constant: 24),
            label.trailingAnchor.constraint(equalTo: viewController.view.trailingAnchor, constant: -24),
            label.centerYAnchor.constraint(equalTo: viewController.view.centerYAnchor),
        ])

        window.rootViewController = viewController
        window.makeKeyAndVisible()
        self.window = window
        statusLabel = label

        let runId = argument(after: "--run-id") ?? "missing"
        let runner = ProbeRunner(runId: runId)
        self.runner = runner
        let resultUrl = FileManager.default.urls(
            for: .documentDirectory,
            in: .userDomainMask
        )[0].appendingPathComponent("blehub-result.txt")
        try? FileManager.default.removeItem(at: resultUrl)

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = runner.run()
            let lifecycle = DispatchQueue.main.sync {
                self?.lifecycleEvents ?? []
            }
            let state = DispatchQueue.main.sync {
                UIApplication.shared.applicationState
            }
            let stateLine = "apple_ios_application_state_at_result=\(state.rawValue)"
            let diagnostic = ([stateLine] + lifecycle).joined(separator: "\n")
            let resultWithLifecycle = result + diagnostic + "\n"
            do {
                try resultWithLifecycle.write(to: resultUrl, atomically: true, encoding: .utf8)
            } catch {
                DispatchQueue.main.async {
                    self?.statusLabel?.text = "Failed to write probe result: \(error)"
                }
                return
            }
            DispatchQueue.main.async {
                self?.statusLabel?.text = result.contains("runtime=passed")
                    ? "BleHub iOS CoreBluetooth runtime passed."
                    : result
            }
        }
        return true
    }
}
