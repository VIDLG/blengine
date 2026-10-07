import UIKit
import CoreFoundation
import React
import React_RCTAppDelegate
import ReactAppDependencyProvider

private func hostReloadNotificationCallback(
  _ center: CFNotificationCenter?,
  _ observer: UnsafeMutableRawPointer?,
  _ name: CFNotificationName?,
  _ object: UnsafeRawPointer?,
  _ userInfo: CFDictionary?
) {
  guard let observer else { return }
  let delegate = Unmanaged<AppDelegate>.fromOpaque(observer).takeUnretainedValue()
  DispatchQueue.main.async { [weak delegate] in
    delegate?.triggerExternalReload()
  }
}

@main
class AppDelegate: UIResponder, UIApplicationDelegate {
  var window: UIWindow?

  var reactNativeDelegate: ReactNativeDelegate?
  private let reloadLock = NSLock()
  private var reloadGateEnabled = false
  private var reloadReadyCount = 0
  private var rfReloadActiveCount = 0
  private let pendingReadGenerationKey = "blehub.nitro.rfPendingReadReloadGeneration"
  private let pendingReadNativeNotification = Notification.Name("dev.blehub.rn.pendingReadNativeBoundary")
  private var pendingReadNativeObserver: NSObjectProtocol?
  private var pendingReadRunId: String?
  private var pendingReadNativeProbeId: String?
  private var pendingReadNativeCharacteristicId: String?
  private var pendingReadNativeDisposed = false
  private var pendingReadNativeLateRejected = false
  private var pendingReadArmReadyEmitted = false
  private var pendingReadNotificationName: String?
  private var pendingReadArmURL: URL?
  private var pendingReadReady = false
  private var pendingReadReloadTriggered = false
  private var pendingReadAwaitingArm = false
  private var pendingReadArmed = false
  private var pendingReadComplete = false
  private var pendingReadFailed = false
  private var externalReloadName: CFNotificationName?
  private var externalReloadEnabled = false

  var reactNativeFactory: RCTReactNativeFactory?

  func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
  ) -> Bool {
    reloadGateEnabled = ProcessInfo.processInfo.arguments.contains("--blehub-nitro-reload") || ProcessInfo.processInfo.arguments.contains("--blehub-nitro-rf-reload-service") || ProcessInfo.processInfo.arguments.contains("--blehub-nitro-rf-pending-read-reload-service")

    // devicectl --console forwards app stdout/stderr, while React Native's
    // console.log normally goes through RCTLog/unified logging on a device.
    // Forward only this spike's structured markers for the device gate; this
    // is host instrumentation and is not part of the production RN module.
    RCTAddLogFunction { [weak self] _, _, _, _, message in
      guard let message, message.contains("BLEHUB_NITRO_RUNTIME") else { return }
      self?.handleRuntimeMarker(message)
    }

    let arguments = ProcessInfo.processInfo.arguments
    let pendingReadRf = arguments.contains("--blehub-nitro-rf-pending-read-reload-service")
    if pendingReadRf {
      UserDefaults.standard.removeObject(forKey: pendingReadGenerationKey)
    }
    if arguments.contains("--blehub-nitro-reload") || pendingReadRf {
      let notificationOption = pendingReadRf
        ? "--blehub-nitro-rf-pending-read-reload-notification" : "--blehub-nitro-reload-notification"
      if let index = arguments.firstIndex(of: notificationOption),
         arguments.indices.contains(index + 1) {
        let name = arguments[index + 1]
        let prefix = "dev.blehub.rn.reload."
        guard name.hasPrefix(prefix), let uuid = UUID(uuidString: String(name.dropFirst(prefix.count))) else {
          Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME \(pendingReadRf ? "rf-pending-read-reload" : "reload"):notification-invalid")
          return false
        }
        if pendingReadRf {
          guard let armURL = URL(string: "blehub-rn-spike://arm/\(uuid.uuidString.lowercased())") else {
            Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:arm-url-invalid")
            return false
          }
          pendingReadNotificationName = name
          pendingReadRunId = uuid.uuidString.lowercased()
          pendingReadArmURL = armURL
          pendingReadNativeObserver = NotificationCenter.default.addObserver(
            forName: pendingReadNativeNotification,
            object: nil,
            queue: nil
          ) { [weak self] notification in
            self?.handlePendingReadNativeBoundary(notification)
          }
        }
        let notificationName = CFNotificationName(rawValue: name as CFString)
        CFNotificationCenterAddObserver(
          CFNotificationCenterGetDarwinNotifyCenter(),
          Unmanaged.passUnretained(self).toOpaque(),
          hostReloadNotificationCallback,
          notificationName.rawValue,
          nil,
          .deliverImmediately
        )
        externalReloadName = notificationName
        externalReloadEnabled = true
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME \(pendingReadRf ? "rf-pending-read-reload" : "reload"):notification-registered")
      } else if pendingReadRf {
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:notification-required")
        return false
      }
    }

    let delegate = ReactNativeDelegate()
    let factory = RCTReactNativeFactory(delegate: delegate)
    delegate.dependencyProvider = RCTAppDependencyProvider()

    reactNativeDelegate = delegate
    reactNativeFactory = factory

    window = UIWindow(frame: UIScreen.main.bounds)

    let ioRf = arguments.contains("--blehub-nitro-rf-io-service")
    let rssiRf = arguments.contains("--blehub-nitro-rf-rssi-service")
    let notificationRf = arguments.contains("--blehub-nitro-rf-notification-service")
    let indicationRf = arguments.contains("--blehub-nitro-rf-indication-service")
    let passiveDisconnectRf = arguments.contains("--blehub-nitro-rf-passive-disconnect-service")
    let recoveryRf = arguments.contains("--blehub-nitro-rf-recovery-service")
    let recoveryTenRf = arguments.contains("--blehub-nitro-rf-recovery-ten-service")
    let reloadRf = arguments.contains("--blehub-nitro-rf-reload-service")
    let multiRf = arguments.contains("--blehub-nitro-rf-multi-service")
    let multiSoakRf = arguments.contains("--blehub-nitro-rf-multi-soak-service")
    let multiFaultRf = arguments.contains("--blehub-nitro-rf-multi-fault-service")
    let multiSubscriptionMode = arguments.firstIndex(of: "--blehub-nitro-rf-multi-subscription-mode").flatMap { index in
      arguments.indices.contains(index + 1) ? arguments[index + 1] : nil
    } ?? "notification"
    let multiCycles = arguments.firstIndex(of: "--blehub-nitro-rf-multi-cycles").flatMap { index in
      arguments.indices.contains(index + 1) ? arguments[index + 1] : nil
    } ?? "10"
    let gattRf = arguments.contains("--blehub-nitro-rf-gatt-service") || ioRf || notificationRf || indicationRf || passiveDisconnectRf || recoveryRf || recoveryTenRf || reloadRf || multiRf || multiSoakRf || multiFaultRf || pendingReadRf
    let connectRf = arguments.contains("--blehub-nitro-rf-connect-service") || gattRf || rssiRf
    let rfOption = rssiRf
      ? "--blehub-nitro-rf-rssi-service"
      : ioRf
      ? "--blehub-nitro-rf-io-service"
      : notificationRf ? "--blehub-nitro-rf-notification-service"
      : indicationRf ? "--blehub-nitro-rf-indication-service"
      : pendingReadRf ? "--blehub-nitro-rf-pending-read-reload-service"
      : multiFaultRf ? "--blehub-nitro-rf-multi-fault-service"
      : multiSoakRf ? "--blehub-nitro-rf-multi-soak-service"
      : multiRf ? "--blehub-nitro-rf-multi-service"
      : reloadRf ? "--blehub-nitro-rf-reload-service"
      : recoveryTenRf ? "--blehub-nitro-rf-recovery-ten-service"
      : recoveryRf ? "--blehub-nitro-rf-recovery-service"
      : passiveDisconnectRf ? "--blehub-nitro-rf-passive-disconnect-service"
      : gattRf ? "--blehub-nitro-rf-gatt-service" : connectRf ? "--blehub-nitro-rf-connect-service" : "--blehub-nitro-rf-service"
    let rfServiceUuid = arguments.firstIndex(of: rfOption).flatMap { index in
      arguments.indices.contains(index + 1) ? arguments[index + 1] : nil
    }
    let runtimeMode = rssiRf
      ? "rf-rssi"
      : ioRf
      ? "rf-io"
      : notificationRf
        ? "rf-notification"
      : indicationRf
        ? "rf-indication"
      : pendingReadRf
        ? "rf-pending-read-reload"
      : multiFaultRf
        ? "rf-multi-fault"
      : multiSoakRf
        ? "rf-multi-soak"
      : multiRf
        ? "rf-multi"
      : reloadRf
        ? "rf-reload"
      : recoveryTenRf
        ? "rf-recovery-ten"
      : recoveryRf
        ? "rf-recovery"
      : passiveDisconnectRf
        ? "rf-passive-disconnect"
      : gattRf
        ? "rf-gatt"
        : connectRf
        ? "rf-connect"
        : arguments.contains("--blehub-nitro-rf-service")
          ? "rf"
          : arguments.contains("--blehub-nitro-stress")
            ? "stress"
            : arguments.contains("--blehub-nitro-reload") ? "reload" : "basic"
    var initialProperties = [
      "runtimeMode": runtimeMode,
      "rfServiceUuid": rfServiceUuid ?? "",
      "rfMultiSubscriptionMode": multiSubscriptionMode,
      "rfMultiCycles": multiCycles,
    ]
    if let pendingReadNotificationName {
      initialProperties["rfReloadNotificationName"] = pendingReadNotificationName
    }
    factory.startReactNative(
      withModuleName: "BleHubNitroHost",
      in: window,
      initialProperties: initialProperties,
      launchOptions: launchOptions
    )

    return true
  }

  private func handleRuntimeMarker(_ message: String) {
    Self.writeRuntimeMarker(message)
    guard reloadGateEnabled else { return }
    if ProcessInfo.processInfo.arguments.contains("--blehub-nitro-rf-pending-read-reload-service") {
      let pendingMarker = "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:generation=1:read-dispatched:"
      if message.contains("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:generation=1:failed:") ||
         message.contains("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:generation=2:failed:") {
        reloadLock.withLock { pendingReadFailed = true }
      } else if message.contains(pendingMarker) {
        let first = reloadLock.withLock { () -> Bool in
          guard !pendingReadReady && !pendingReadReloadTriggered && !pendingReadFailed,
                let characteristicId = pendingReadNativeCharacteristicId,
                message.contains("commandId=12008:connectionId=12004:gattSessionId=12006:characteristicId=\(characteristicId):peripheralId=")
          else { return false }
          pendingReadReady = true
          return true
        }
        if first {
          // AppDelegate survives a JS engine reload; Settings in the new JS
          // engine reads this value from RN's standard NSUserDefaults store.
          UserDefaults.standard.set(2, forKey: pendingReadGenerationKey)
          Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:external-ready")
        } else {
          reloadLock.withLock { pendingReadFailed = true }
          Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:unmatched-or-duplicate-read-dispatch")
        }
      } else if message.contains("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:generation=2:awaiting-arm:") {
        let ready = reloadLock.withLock { () -> Bool in
          guard pendingReadReloadTriggered && !pendingReadAwaitingArm && !pendingReadArmed && !pendingReadFailed else { return false }
          pendingReadAwaitingArm = true
          return true
        }
        if ready {
          announcePendingReadArmReadyIfPossible()
        } else {
          reloadLock.withLock { pendingReadFailed = true }
          Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:unexpected-awaiting-arm")
        }
      } else if message.contains("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:generation=2:read:passed:") {
        let complete = reloadLock.withLock { () -> Bool in
          guard pendingReadReloadTriggered && pendingReadArmed && !pendingReadComplete && !pendingReadFailed else { return false }
          pendingReadComplete = true
          return true
        }
        if complete {
          Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:complete:passed")
        } else {
          Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:unexpected-generation-2-read")
        }
      }
      return
    }
    if ProcessInfo.processInfo.arguments.contains("--blehub-nitro-rf-reload-service") {

      guard message.contains("BLEHUB_NITRO_RUNTIME rf-reload:active:") else { return }
      let count = reloadLock.withLock { () -> Int in
        rfReloadActiveCount += 1
        return rfReloadActiveCount
      }
      if count == 1 {
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-reload:native-triggered")
        // Give the host/fixture time to arm GAP observation; the fixture fails
        // closed if the host disconnect arrives before its armed boundary.
        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) {
          RCTTriggerReloadCommandListeners("BleHub active RF reload gate")
        }
      }
      return
    }
    guard message.contains("BLEHUB_NITRO_RUNTIME reload:ready") else { return }

    let readyCount = reloadLock.withLock {
      reloadReadyCount += 1
      return reloadReadyCount
    }
    if readyCount == 1 {
      if externalReloadEnabled {
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:external-ready")
      } else {
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:native-triggered")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
          RCTTriggerReloadCommandListeners("BleHub Nitro reload gate")
        }
      }
    } else if readyCount == 2 {
      Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:complete:passed")
    }
  }

  private func handlePendingReadNativeBoundary(_ notification: Notification) {
    guard let fields = notification.userInfo,
          let event = fields["event"] as? String,
          let runId = fields["runId"] as? String,
          let probeId = fields["probeId"] as? String,
          let commandId = fields["commandId"] as? String,
          let connectionId = fields["connectionId"] as? String,
          let gattSessionId = fields["gattSessionId"] as? String,
          let characteristicId = fields["characteristicId"] as? String else {
      reloadLock.withLock { pendingReadFailed = true }
      Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:native-boundary-malformed")
      return
    }
    let outcome = reloadLock.withLock { () -> String in
      guard !pendingReadFailed,
            runId == pendingReadRunId,
            UUID(uuidString: probeId)?.uuidString.lowercased() == probeId,
            commandId == "12008", connectionId == "12004", gattSessionId == "12006",
            let characteristicValue = UInt64(characteristicId), characteristicValue > 0,
            String(characteristicValue) == characteristicId else {
        pendingReadFailed = true
        return "identity-mismatch"
      }
      switch event {
      case "read-accepted":
        guard pendingReadNativeProbeId == nil && !pendingReadReady else {
          pendingReadFailed = true
          return "duplicate-read-accepted"
        }
        pendingReadNativeProbeId = probeId
        pendingReadNativeCharacteristicId = characteristicId
      case "dispose-returned":
        guard pendingReadReloadTriggered && !pendingReadNativeDisposed,
              pendingReadNativeProbeId == probeId,
              pendingReadNativeCharacteristicId == characteristicId else {
          pendingReadFailed = true
          return "unexpected-dispose"
        }
        pendingReadNativeDisposed = true
      case "late-submit-rejected":
        guard pendingReadNativeDisposed && !pendingReadNativeLateRejected,
              pendingReadNativeProbeId == probeId,
              pendingReadNativeCharacteristicId == characteristicId else {
          pendingReadFailed = true
          return "unexpected-late-submit"
        }
        pendingReadNativeLateRejected = true
      case "dispose-failed":
        pendingReadFailed = true
        return "dispose-failed"
      default:
        pendingReadFailed = true
        return "unknown-event"
      }
      return "accepted"
    }
    guard outcome == "accepted" else {
      Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:native-boundary-\(outcome)")
      return
    }
    Self.writeRuntimeMarker(
      "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:native:\(event):runId=\(runId):probeId=\(probeId):commandId=\(commandId):connectionId=\(connectionId):gattSessionId=\(gattSessionId):characteristicId=\(characteristicId)"
    )
    if event == "late-submit-rejected" {
      announcePendingReadArmReadyIfPossible()
    }
  }

  private func announcePendingReadArmReadyIfPossible() {
    let ready = reloadLock.withLock { () -> Bool in
      guard pendingReadAwaitingArm && pendingReadNativeDisposed && pendingReadNativeLateRejected &&
            !pendingReadArmReadyEmitted && !pendingReadFailed else { return false }
      pendingReadArmReadyEmitted = true
      return true
    }
    if ready {
      Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:arm-ready")
    }
  }

  private func removeExternalReloadObserver() {
    externalReloadEnabled = false
    if let name = externalReloadName {
      externalReloadName = nil
      CFNotificationCenterRemoveObserver(
        CFNotificationCenterGetDarwinNotifyCenter(),
        Unmanaged.passUnretained(self).toOpaque(),
        name,
        nil
      )
    }
  }

  fileprivate func triggerExternalReload() {
    if ProcessInfo.processInfo.arguments.contains("--blehub-nitro-rf-pending-read-reload-service") {
      // The same Darwin observer must survive the first post and the RN reload.
      // A queued extra delivery after removal is still a failure, not a third arm.
      let outcome = reloadLock.withLock { () -> String in
        if !externalReloadEnabled || pendingReadArmed {
          pendingReadFailed = true
          return "duplicate-arm-notification"
        }
        if pendingReadFailed { return "notification-after-failure" }
        if !pendingReadReloadTriggered {
          guard pendingReadReady else {
            pendingReadFailed = true
            return "notification-before-read-dispatch"
          }
          pendingReadReloadTriggered = true
          return "reload"
        }
        guard pendingReadArmReadyEmitted && pendingReadNativeDisposed && pendingReadNativeLateRejected else {
          pendingReadFailed = true
          return "notification-before-native-dispose-and-arm-ready"
        }
        pendingReadArmed = true
        return "arm"
      }
      switch outcome {
      case "reload":
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:notification-received")
        RCTTriggerReloadCommandListeners("BleHub pending read external RF reload gate")
      case "arm":
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:arm-notification-received")
        guard let armURL = pendingReadArmURL,
              RCTLinkingManager.application(UIApplication.shared, open: armURL, options: [:]) else {
          reloadLock.withLock { pendingReadFailed = true }
          removeExternalReloadObserver()
          Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:arm-linking-dispatch-rejected")
          return
        }
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:arm-linking-dispatched:accepted=true")
        removeExternalReloadObserver()
      default:
        removeExternalReloadObserver()
        Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:\(outcome)")
      }
      return
    }
    guard externalReloadEnabled else { return }
    removeExternalReloadObserver()
    let ready = reloadLock.withLock { reloadReadyCount == 1 }
    guard ready else {
      Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:notification-before-ready")
      return
    }
    Self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:notification-received")
    RCTTriggerReloadCommandListeners("BleHub host-triggered reload spike")
  }

  deinit {
    if let pendingReadNativeObserver {
      NotificationCenter.default.removeObserver(pendingReadNativeObserver)
    }
    if let name = externalReloadName {
      CFNotificationCenterRemoveObserver(
        CFNotificationCenterGetDarwinNotifyCenter(),
        Unmanaged.passUnretained(self).toOpaque(),
        name,
        nil
      )
    }
  }

  private static func writeRuntimeMarker(_ message: String) {
    guard let data = (message + "\n").data(using: .utf8) else { return }
    FileHandle.standardError.write(data)
  }
}

class ReactNativeDelegate: RCTDefaultReactNativeFactoryDelegate {
  override func sourceURL(for bridge: RCTBridge) -> URL? {
    self.bundleURL()
  }

  override func bundleURL() -> URL? {
#if DEBUG
    RCTBundleURLProvider.sharedSettings().jsBundleURL(forBundleRoot: "index")
#else
    Bundle.main.url(forResource: "main", withExtension: "jsbundle")
#endif
  }
}
