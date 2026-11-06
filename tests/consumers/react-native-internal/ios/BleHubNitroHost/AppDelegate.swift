import UIKit
import CoreBluetooth
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

private func rfControlNotificationCallback(
  _ center: CFNotificationCenter?, _ observer: UnsafeMutableRawPointer?,
  _ name: CFNotificationName?, _ object: UnsafeRawPointer?, _ userInfo: CFDictionary?
) {
  guard let observer, let name else { return }
  let delegate = Unmanaged<AppDelegate>.fromOpaque(observer).takeUnretainedValue()
  // Validate readiness on receipt, not after a deferred main-queue hop.
  delegate.deliverRfControl(name.rawValue as String)
}

private struct RfControlIdentity: Encodable {
  let session: String
  let flow: Int
  let mode: String
  let token: String
  let bundle_id: String
  let pid: Int32
  let boot_id: String
  let executable: String
  var phase: String?
}

private struct RecoveryHostIdentity: Encodable {
  let run_id: String
  let pid: Int32
  let boot_id: String
  let bundle_id: String
  let executable: String
  var cycle: Int?
  var source: String?
  var url: String?
}

@main
class AppDelegate: UIResponder, UIApplicationDelegate {
  var window: UIWindow?
  private let hostBootId = UUID().uuidString.lowercased()
  private var adapterStartup: AdapterStartupSession?
  private var fullRecovery: FullRecoveryBeginGate?
  private var recoveryIdentity: RecoveryHostIdentity?
  private var recoveryNextCycle = 0
  private var recoveryAwaitingCycle: Int?
  private var recoveryNonce: String?
  private var recoveryRuntimeClosed = false
  private var recoveryFailed = false
  private let recoveryLock = NSLock()

  private func failRecovery(_ reason: String) {
    recoveryLock.withLock { recoveryFailed = true }
    if fullRecovery != nil {
      self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-lab-full:failed:native:\(reason)")
    }
    self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-recovery-ten:native:failed:\(reason)")
  }

  private func writeRecoveryRecord(_ kind: String, identity: RecoveryHostIdentity) {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    guard let data = try? encoder.encode(identity), let json = String(data: data, encoding: .utf8) else {
      failRecovery("identity-encoding")
      return
    }
    let prefix = kind == "begin" ? "rf-lab-full:native:signal" : "rf-recovery-ten:native:\(kind)"
    self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME \(prefix):\(json)")
  }

  func application(_ application: UIApplication, open url: URL,
                   options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
    if let adapterStartup {
      let accepted = adapterStartup.deliver(url.absoluteString, record: { self.writeRuntimeMarker($0) }, forward: {
        RCTLinkingManager.application(application, open: url, options: options)
      })
      if !accepted { self.writeRuntimeMarker(AdapterStartupGate.prefix + "failed:native:url-rejected") }
      return accepted
    }
    if rfControlLock.withLock({ writeRejectControl != nil || cccEnableRejectControl != nil || cccDisableRejectControl != nil }) {
      guard let name = rfControlURIs.first(where: { $0.value.absoluteString == url.absoluteString })?.key else {
        failRfControl("foreign-payload-url-or-reload"); return false
      }
      return deliverRfControl(name)
    }
    if BleHubPendingReloadHost.enabled {
      guard let action = BleHubPendingReloadHost.acceptControl(url.absoluteString) else { return false }
      if action == "reload" {
        RCTTriggerReloadCommandListeners("BleHub pending read payload URL reload")
        return true
      }
      let accepted = RCTLinkingManager.application(application, open: url, options: options)
      if !accepted { BleHubPendingReloadHost.fail("arm-linking-rejected") }
      return accepted
    }
    guard recoveryIdentity != nil else {
      return RCTLinkingManager.application(application, open: url, options: options)
    }
    if fullRecovery != nil && !recoveryLock.withLock({ fullRecovery!.begun }) {
      let record = recoveryLock.withLock { () -> RecoveryHostIdentity? in
        let complete = rfControlLock.withLock {
          !rfControlFailed && rfControlNextPhase == rfControlSteps.count && rfControlDelivered == rfControlSteps.count
        }
        guard !recoveryFailed, var identity = recoveryIdentity,
              fullRecovery!.acceptBegin(url.absoluteString, controlsComplete: complete) else { return nil }
        identity.source = "payload-url"
        identity.url = url.absoluteString
        return identity
      }
      guard let record else { failRecovery("foreign-early-or-duplicate-begin-url"); return false }
      writeRecoveryRecord("begin", identity: record)
      guard RCTLinkingManager.application(application, open: url, options: options) else {
        failRecovery("begin-linking-rejected"); return false
      }
      return true
    }
    let record = recoveryLock.withLock { () -> RecoveryHostIdentity? in
      guard fullRecovery == nil || (!fullRecovery!.failed && !fullRecovery!.closed && fullRecovery!.begun),
            !recoveryFailed, !recoveryRuntimeClosed, recoveryNonce != nil,
            var identity = recoveryIdentity, recoveryNextCycle < 10,
            recoveryAwaitingCycle == recoveryNextCycle,
            url.absoluteString == "blehub-rn-spike://recover/\(identity.run_id)/\(recoveryNextCycle)" else { return nil }
      identity.cycle = recoveryNextCycle
      identity.source = "payload-url"
      identity.url = url.absoluteString
      recoveryNextCycle += 1
      recoveryAwaitingCycle = nil
      return identity
    }
    guard let record else { failRecovery("foreign-early-or-duplicate-payload-url"); return false }
    writeRecoveryRecord("signal", identity: record)
    guard RCTLinkingManager.application(application, open: url, options: options) else {
      failRecovery("linking-rejected")
      return false
    }
    return true
  }

  private func observeRecoveryMarker(_ message: String) {
    guard let identity = recoveryIdentity else { return }
    if fullRecovery != nil {
      recoveryLock.withLock { fullRecovery!.observe(message) }
      if recoveryLock.withLock({ fullRecovery!.failed }) { failRecovery("full-suite-phase-or-nonce") }
    }
    let runtimePrefix = "BLEHUB_NITRO_RUNTIME rf-recovery-ten:runtime:runId=\(identity.run_id):nonce="
    if let range = message.range(of: runtimePrefix) {
      let payload = String(message[range.upperBound...])
      let parts = payload.components(separatedBy: ":phase=")
      let valid = recoveryLock.withLock { () -> Bool in
        guard !recoveryFailed, parts.count == 2, !parts[0].isEmpty,
              fullRecovery == nil || (fullRecovery!.begun && !fullRecovery!.closed && fullRecovery!.nonce == parts[0]) else { return false }
        if parts[1] == "start" && recoveryNonce == nil && !recoveryRuntimeClosed {
          recoveryNonce = parts[0]
          return true
        }
        if parts[1] == "close" && recoveryNonce == parts[0] && !recoveryRuntimeClosed {
          recoveryRuntimeClosed = true
          return true
        }
        return false
      }
      if !valid { failRecovery("JS-nonce-or-runtime-phase") }
    } else if message.contains("BLEHUB_NITRO_RUNTIME rf-recovery-ten:runtime:") {
      failRecovery("foreign-JS-runtime")
    }
    if message.contains(":recovery-awaiting:") && message.contains("rf-recovery-ten:cycle:") {
      let valid = recoveryLock.withLock { () -> Bool in
        guard !recoveryFailed, !recoveryRuntimeClosed, recoveryNonce != nil,
              recoveryAwaitingCycle == nil, recoveryNextCycle < 10,
              message.hasSuffix("rf-recovery-ten:cycle:\(recoveryNextCycle):recovery-awaiting:runId=\(identity.run_id)") else { return false }
        recoveryAwaitingCycle = recoveryNextCycle
        return true
      }
      if !valid { failRecovery("foreign-early-or-duplicate-JS-awaiting") }
    }
  }
  private var rfControlURIs: [String: URL] = [:]
  private var rfControlIdentity: RfControlIdentity?
  private var writeRejectControl: NarrowRejectControlGate?
  private var cccEnableRejectControl: NarrowRejectControlGate?
  private var cccDisableRejectControl: NarrowRejectControlGate?
  private var rfControlSteps: [(identity: RfControlIdentity, phase: String, request: String, listener: String)] = []
  private var rfControlListenerReady = false
  private var rfControlNextPhase = 0
  private var rfControlDelivered = 0
  private var rfControlReadyPhase: String?
  private var rfControlFailed = false
  private let rfControlLock = NSLock()

  private func failRfControl(_ reason: String) {
    let writeReject = rfControlLock.withLock { () -> Bool in
      rfControlFailed = true
      writeRejectControl?.fail()
      cccEnableRejectControl?.fail()
      cccDisableRejectControl?.fail()
      return writeRejectControl != nil
    }
    if cccDisableRejectControl != nil { self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-ccc-disable-reject:failed:native:\(reason)") }
    if cccEnableRejectControl != nil { self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-ccc-enable-reject:failed:native:\(reason)") }
    if writeReject { self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-write-reject:failed:native:\(reason)") }
    self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-control:host-failed:\(reason)")
  }

  private func writeRfHostRecord(_ kind: String, phase: String? = nil, identity: RfControlIdentity? = nil) {
    guard var record = identity ?? rfControlIdentity else { return }
    record.phase = phase
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    guard let data = try? encoder.encode(record), let json = String(data: data, encoding: .utf8) else {
      failRfControl("identity-encoding")
      return
    }
    self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-host:\(kind):\(json)")
  }

  @discardableResult
  fileprivate func deliverRfControl(_ name: String) -> Bool {
    if recoveryLock.withLock({ fullRecovery.map { $0.begun || $0.closed || $0.failed } ?? false }) {
      failRfControl("control-after-full-suite-transition")
      failRecovery("control-after-full-suite-transition")
      return false
    }
    let target = rfControlLock.withLock { () -> (URL, RfControlIdentity, String)? in
      guard !rfControlFailed, rfControlListenerReady,
            rfControlNextPhase < rfControlSteps.count,
            let uri = rfControlURIs[name] else { return nil }
      let step = rfControlSteps[rfControlNextPhase]
      guard uri.pathComponents.last == step.phase,
            uri.absoluteString == "blehub-rf://control/\(step.identity.session)/\(step.identity.flow)/\(step.identity.mode)/\(step.identity.token)/\(step.phase)",
            rfControlReadyPhase == step.phase else { return nil }
      if writeRejectControl != nil && !writeRejectControl!.accept(uri.absoluteString) { return nil }
      if cccEnableRejectControl != nil && !cccEnableRejectControl!.accept(uri.absoluteString) { return nil }
      if cccDisableRejectControl != nil && !cccDisableRejectControl!.accept(uri.absoluteString) { return nil }
      rfControlReadyPhase = nil
      rfControlNextPhase += 1
      if rfControlNextPhase < rfControlSteps.count && rfControlSteps[rfControlNextPhase].identity.flow != step.identity.flow {
        rfControlListenerReady = false
      }
      return (uri, step.identity, step.phase)
    }
    guard let (uri, identity, phase) = target else {
      failRfControl("foreign-early-or-duplicate-signal")
      return false
    }
    DispatchQueue.main.async { [weak self] in
      guard let self, !self.rfControlLock.withLock({ self.rfControlFailed }) else { return }
      // Publish native acceptance before Linking can synchronously produce a JS ACK.
      self.writeRfHostRecord("signal", phase: phase, identity: identity)
      guard RCTLinkingManager.application(UIApplication.shared, open: uri, options: [:]) else {
        self.failRfControl("linking-rejected")
        return
      }
      self.rfControlLock.withLock { self.rfControlDelivered += 1 }
    }
    return true
  }

  private func installRfControl(_ raw: String, standaloneMode: String? = nil) -> Bool {
    guard let data = raw.data(using: .utf8),
          let bundle = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let bundleId = Bundle.main.bundleIdentifier, let executable = Bundle.main.executableURL?.absoluteString else { return false }
    let controls: [[String: Any]]
    if let standaloneMode {
      guard standaloneMode == "rf-watchdog" || standaloneMode == "rf-cancellation" || standaloneMode == "rf-write-reject" || standaloneMode == "rf-ccc-enable-reject" || standaloneMode == "rf-ccc-disable-reject",
            Set(bundle.keys) == Set(["session", "flow", "mode", "token"]) else { return false }
      if standaloneMode == "rf-write-reject" {
        guard let gate = NarrowRejectControlGate(raw) else { return false }
        writeRejectControl = gate
      }
      if standaloneMode == "rf-ccc-enable-reject" {
        guard let gate = NarrowRejectControlGate(raw, mode: standaloneMode) else { return false }
        cccEnableRejectControl = gate
      }
      if standaloneMode == "rf-ccc-disable-reject" {
        guard let gate = NarrowRejectControlGate(raw, mode: standaloneMode) else { return false }
        cccDisableRejectControl = gate
      }
      controls = [bundle]
    } else {
      guard Set(bundle.keys) == Set(["controls"]),
            let values = bundle["controls"] as? [[String: Any]], values.count == 2 else { return false }
      controls = values
    }
    let bootId = hostBootId
    var previousSession: String?
    var tokens = Set<String>()
    for (index, value) in controls.enumerated() {
      let mode = standaloneMode ?? (index == 0 ? "rf-cancellation" : "rf-watchdog")
      let flow = mode == "rf-ccc-disable-reject" ? 12 : mode == "rf-ccc-enable-reject" ? 11 : mode == "rf-write-reject" ? 10 : mode == "rf-cancellation" ? 8 : 9
      guard Set(value.keys) == Set(["session", "flow", "mode", "token"]),
            let session = value["session"] as? String, UUID(uuidString: session)?.uuidString.lowercased() == session,
            let token = value["token"] as? String, UUID(uuidString: token)?.uuidString.lowercased() == token,
            value["flow"] as? Int == flow, value["mode"] as? String == mode,
            previousSession == nil || previousSession == session, tokens.insert(token).inserted else { return false }
      previousSession = session
      let identity = RfControlIdentity(session: session, flow: flow, mode: mode, token: token,
        bundle_id: bundleId, pid: ProcessInfo.processInfo.processIdentifier, boot_id: bootId, executable: executable)
      if rfControlIdentity == nil { rfControlIdentity = identity }
      let phases = mode == "rf-ccc-disable-reject" ? NarrowRejectControlGate.cccDisablePhases : mode == "rf-ccc-enable-reject" ? NarrowRejectControlGate.cccPhases : mode == "rf-write-reject" ? ["armed", "held", "rejected", "close-allowed"] : mode == "rf-cancellation" ? ["armed", "held", "released"] : ["armed", "held", "released", "recovery"]
      let requests = mode == "rf-ccc-disable-reject" ? NarrowRejectControlGate.cccDisableRequests : mode == "rf-ccc-enable-reject" ? NarrowRejectControlGate.cccRequests : mode == "rf-write-reject" ? ["arm", "write-dispatched", "reject", "recovery-done"] : mode == "rf-cancellation" ? ["arm", "read-dispatched", "release"] : ["arm", "read-dispatched", "release", "recover"]
      let listener = "rf-control:listener:session=\(session):flow=\(flow):mode=\(mode):token=\(token):phase=registered"
      for (phase, request) in zip(phases, requests) {
        let name = "dev.blehub.rf.control.\(session).\(flow).\(mode).\(token).\(phase)"
        guard let uri = URL(string: "blehub-rf://control/\(session)/\(flow)/\(mode)/\(token)/\(phase)") else { return false }
        rfControlURIs[name] = uri
        rfControlSteps.append((identity, phase, request, listener))
        CFNotificationCenterAddObserver(
          CFNotificationCenterGetDarwinNotifyCenter(), Unmanaged.passUnretained(self).toOpaque(),
          rfControlNotificationCallback, name as CFString, nil, .deliverImmediately
        )
      }
    }
    writeRfHostRecord("bootstrap")
    return !rfControlFailed
  }

  var reactNativeDelegate: ReactNativeDelegate?
  private let reloadLock = NSLock()
  private var reloadGateEnabled = false
  private var reloadReadyCount = 0
  private var rfReloadActiveCount = 0
  private var externalReloadName: CFNotificationName?
  private var externalReloadEnabled = false

  var reactNativeFactory: RCTReactNativeFactory?
  private var pendingReactNativeFactory: RCTReactNativeFactory?
  private var pendingReactNativeProperties: [String: Any]?
  private var pendingReactNativeLaunchOptions: [UIApplication.LaunchOptionsKey: Any]?

  func connect(window: UIWindow) {
    self.window = window
    guard let factory = pendingReactNativeFactory,
          let properties = pendingReactNativeProperties else { return }
    pendingReactNativeFactory = nil
    pendingReactNativeProperties = nil
    let launchOptions = pendingReactNativeLaunchOptions
    pendingReactNativeLaunchOptions = nil
    factory.startReactNative(
      withModuleName: "BleHubNitroHost",
      in: window,
      initialProperties: properties,
      launchOptions: launchOptions
    )
  }

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
    var adapterLaunch: AdapterStartupLaunch?
    if AdapterStartupLaunch.requested(arguments) {
      guard let launch = AdapterStartupLaunch(arguments: arguments,
        authorized: CBManager.authorization == .allowedAlways,
        pid: ProcessInfo.processInfo.processIdentifier, bootId: hostBootId,
        bundleId: Bundle.main.bundleIdentifier, executable: Bundle.main.executableURL),
        launchOptions?[.url] == nil else {
        self.writeRuntimeMarker(AdapterStartupGate.prefix + "failed:native:invalid-or-unauthorized-launch")
        return false
      }
      do {
        let caches = try FileManager.default.url(for: .cachesDirectory, in: .userDomainMask,
          appropriateFor: nil, create: true)
        let journal = try AdapterMarkerJournal(directory: caches.appendingPathComponent("blehub-ios-adapter", isDirectory: true),
          runId: launch.identity.run_id)
        adapterStartup = AdapterStartupSession(identity: launch.identity, journal: journal)
        adapterLaunch = launch
        guard self.writeRuntimeMarker(AdapterStartupGate.prefix + "native:bootstrap:" + launch.properties["rfAdapterNativeIdentity"]!) else { return false }
      } catch {
        self.writeRuntimeMarker(AdapterStartupGate.prefix + "failed:native:journal-create-or-existing-attempt")
        return false
      }
    }
    let writeRejectOption = "--blehub-nitro-rf-write-reject-control"
    let writeRejectService = "--blehub-nitro-rf-write-reject-service"
    var rfWriteRejectControl: String?
    if arguments.contains(writeRejectOption) || arguments.contains(writeRejectService) {
      guard arguments.filter({ $0 == writeRejectOption }).count == 1,
            arguments.filter({ $0 == writeRejectService }).count == 1,
            arguments.filter({ $0.hasPrefix("--blehub-nitro-") && $0.hasSuffix("-service") }).count == 1,
            !arguments.contains(where: { $0.contains("reload") || $0.contains("recovery-run-id") || $0.contains("permission-phase") }),
            !arguments.contains("--blehub-nitro-basic"), !arguments.contains("--blehub-nitro-stress"),
            !arguments.contains("--blehub-nitro-rf-cancellation-control"),
            !arguments.contains("--blehub-nitro-rf-ccc-disable-reject-control"),
            !arguments.contains("--blehub-nitro-rf-ccc-enable-reject-control"),
            !arguments.contains("--blehub-nitro-rf-lab-suite-flow"),
            let index = arguments.firstIndex(of: writeRejectOption), arguments.indices.contains(index + 1),
            let serviceIndex = arguments.firstIndex(of: writeRejectService), arguments.indices.contains(serviceIndex + 1),
            UUID(uuidString: arguments[serviceIndex + 1])?.uuidString.lowercased() == arguments[serviceIndex + 1],
            let nameIndex = arguments.firstIndex(of: "--blehub-nitro-rf-expected-local-name"), arguments.indices.contains(nameIndex + 1),
            !arguments[nameIndex + 1].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            installRfControl(arguments[index + 1], standaloneMode: "rf-write-reject") else {
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-write-reject:failed:native:invalid-launch-metadata")
        return false
      }
      rfWriteRejectControl = arguments[index + 1]
    }
    let cccRejectOption = "--blehub-nitro-rf-ccc-enable-reject-control"
    let cccRejectService = "--blehub-nitro-rf-ccc-enable-reject-service"
    var rfCccEnableRejectControl: String?
    if arguments.contains(cccRejectOption) || arguments.contains(cccRejectService) {
      guard arguments.filter({ $0 == cccRejectOption }).count == 1,
            arguments.filter({ $0 == cccRejectService }).count == 1,
            arguments.filter({ $0.hasPrefix("--blehub-nitro-") && $0.hasSuffix("-service") }).count == 1,
            !arguments.contains(where: { $0.contains("reload") || $0.contains("recovery-run-id") || $0.contains("permission-phase") }),
            !arguments.contains("--blehub-nitro-basic"), !arguments.contains("--blehub-nitro-stress"),
            !arguments.contains("--blehub-nitro-rf-cancellation-control"),
            !arguments.contains("--blehub-nitro-rf-ccc-disable-reject-control"),
            !arguments.contains("--blehub-nitro-rf-write-reject-control"),
            !arguments.contains("--blehub-nitro-rf-lab-suite-flow"),
            let index = arguments.firstIndex(of: cccRejectOption), arguments.indices.contains(index + 1),
            let serviceIndex = arguments.firstIndex(of: cccRejectService), arguments.indices.contains(serviceIndex + 1),
            UUID(uuidString: arguments[serviceIndex + 1])?.uuidString.lowercased() == arguments[serviceIndex + 1],
            let nameIndex = arguments.firstIndex(of: "--blehub-nitro-rf-expected-local-name"), arguments.indices.contains(nameIndex + 1),
            !arguments[nameIndex + 1].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            installRfControl(arguments[index + 1], standaloneMode: "rf-ccc-enable-reject") else {
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-ccc-enable-reject:failed:native:invalid-launch-metadata")
        return false
      }
      rfCccEnableRejectControl = arguments[index + 1]
    }
    let cccDisableOption = "--blehub-nitro-rf-ccc-disable-reject-control"
    let cccDisableService = "--blehub-nitro-rf-ccc-disable-reject-service"
    var rfCccDisableRejectControl: String?
    if arguments.contains(cccDisableOption) || arguments.contains(cccDisableService) {
      let nameOption = "--blehub-nitro-rf-expected-local-name"
      let nameIndex = arguments.firstIndex(of: nameOption)
      let nameValid = !arguments.contains(nameOption) || (arguments.filter { $0 == nameOption }.count == 1 &&
        nameIndex.map { arguments.indices.contains($0 + 1) && !arguments[$0 + 1].hasPrefix("--") &&
          !arguments[$0 + 1].trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && arguments[$0 + 1].count <= 128 } == true)
      guard arguments.filter({ $0 == cccDisableOption }).count == 1,
            arguments.filter({ $0 == cccDisableService }).count == 1,
            arguments.filter({ $0.hasPrefix("--blehub-nitro-") && $0.hasSuffix("-service") }).count == 1,
            !arguments.contains(where: { $0.contains("reload") || $0.contains("recovery-run-id") || $0.contains("permission-phase") }),
            !arguments.contains("--blehub-nitro-basic"), !arguments.contains("--blehub-nitro-stress"),
            !arguments.contains("--blehub-nitro-rf-cancellation-control"),
            !arguments.contains("--blehub-nitro-rf-write-reject-control"),
            !arguments.contains("--blehub-nitro-rf-ccc-enable-reject-control"),
            !arguments.contains("--blehub-nitro-rf-lab-suite-flow"), nameValid,
            let index = arguments.firstIndex(of: cccDisableOption), arguments.indices.contains(index + 1),
            let serviceIndex = arguments.firstIndex(of: cccDisableService), arguments.indices.contains(serviceIndex + 1),
            UUID(uuidString: arguments[serviceIndex + 1])?.uuidString.lowercased() == arguments[serviceIndex + 1],
            installRfControl(arguments[index + 1], standaloneMode: "rf-ccc-disable-reject") else {
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-ccc-disable-reject:failed:native:invalid-launch-metadata")
        return false
      }
      rfCccDisableRejectControl = arguments[index + 1]
    }
    var rfCancellationControl: String?
    if let index = arguments.firstIndex(of: "--blehub-nitro-rf-cancellation-control") {
      let lanes = ["--blehub-nitro-rf-lab-suite-service", "--blehub-nitro-rf-watchdog-service", "--blehub-nitro-rf-cancellation-service"].filter { arguments.contains($0) }
      let standaloneMode = arguments.contains("--blehub-nitro-rf-watchdog-service") ? "rf-watchdog" : arguments.contains("--blehub-nitro-rf-cancellation-service") ? "rf-cancellation" : nil
      guard arguments.filter({ $0 == "--blehub-nitro-rf-cancellation-control" }).count == 1,
            arguments.indices.contains(index + 1), lanes.count == 1,
            installRfControl(arguments[index + 1], standaloneMode: standaloneMode) else {
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-control:host-failed:invalid-metadata")
        return false
      }
      rfCancellationControl = arguments[index + 1]
    }
    let pendingReadRf = arguments.contains("--blehub-nitro-rf-pending-read-reload-service")
    if pendingReadRf {
      let option = "--blehub-nitro-rf-pending-read-reload-notification"
      guard arguments.filter({ $0 == option }).count == 1,
            let index = arguments.firstIndex(of: option), arguments.indices.contains(index + 1) else { return false }
      let name = arguments[index + 1]
      let prefix = "dev.blehub.rn.reload."
      guard name.hasPrefix(prefix) else { return false }
      let runId = String(name.dropFirst(prefix.count))
      let explicit = "--blehub-nitro-rf-pending-read-reload-run-id"
      if let index = arguments.firstIndex(of: explicit) {
        guard arguments.filter({ $0 == explicit }).count == 1, arguments.indices.contains(index + 1),
              arguments[index + 1] == runId else { return false }
      }
      guard BleHubPendingReloadHost.configure(runId: runId, notificationName: name, bootId: hostBootId) else { return false }
    }
    if arguments.contains("--blehub-nitro-reload") {
      let notificationOption = pendingReadRf
        ? "--blehub-nitro-rf-pending-read-reload-notification" : "--blehub-nitro-reload-notification"
      if let index = arguments.firstIndex(of: notificationOption),
         arguments.indices.contains(index + 1) {
        let name = arguments[index + 1]
        let prefix = "dev.blehub.rn.reload."
        guard name.hasPrefix(prefix), UUID(uuidString: String(name.dropFirst(prefix.count))) != nil else {
          self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME \(pendingReadRf ? "rf-pending-read-reload" : "reload"):notification-invalid")
          return false
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
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME \(pendingReadRf ? "rf-pending-read-reload" : "reload"):notification-registered")
      } else if pendingReadRf {
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-pending-read-reload:failed:notification-required")
        return false
      }
    }

    let delegate = ReactNativeDelegate()
    let factory = RCTReactNativeFactory(delegate: delegate)
    delegate.dependencyProvider = RCTAppDependencyProvider()

    reactNativeDelegate = delegate
    reactNativeFactory = factory

    let permissionCheck = arguments.contains("--blehub-nitro-permission-phase")
    let writeRejectRf = rfWriteRejectControl != nil
    let cccRejectRf = rfCccEnableRejectControl != nil
    let cccDisableRf = rfCccDisableRejectControl != nil
    let watchdogRf = arguments.contains("--blehub-nitro-rf-watchdog-service")
    let cancellationRf = arguments.contains("--blehub-nitro-rf-cancellation-service")
    let rfLabSuite = arguments.contains("--blehub-nitro-rf-lab-suite-service")
    let rfLab = arguments.contains("--blehub-nitro-rf-lab-service")
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
    let rfOption = cccDisableRf ? cccDisableService : cccRejectRf ? cccRejectService : writeRejectRf ? writeRejectService : permissionCheck ? "--blehub-nitro-permission-service" : watchdogRf
      ? "--blehub-nitro-rf-watchdog-service"
      : cancellationRf ? "--blehub-nitro-rf-cancellation-service"
      : rfLabSuite
      ? "--blehub-nitro-rf-lab-suite-service"
      : rfLab
      ? "--blehub-nitro-rf-lab-service"
      : rssiRf
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
    let rfLabSuiteFlow = arguments.firstIndex(of: "--blehub-nitro-rf-lab-suite-flow").flatMap { index in
      arguments.indices.contains(index + 1) ? arguments[index + 1] : nil
    }
    let runtimeMode = adapterLaunch != nil ? "rf-adapter-transition" : cccDisableRf ? "rf-ccc-disable-reject" : cccRejectRf ? "rf-ccc-enable-reject" : writeRejectRf ? "rf-write-reject" : permissionCheck ? "permission-authorization" : watchdogRf
      ? "rf-watchdog"
      : cancellationRf ? "rf-cancellation"
      : rfLabSuite
      ? "rf-lab-suite"
      : rfLab
      ? "rf-lab"
      : rssiRf
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
            : arguments.contains("--blehub-nitro-reload") ? "reload"
              : arguments.contains("--blehub-nitro-basic") ? "basic" : "rf-workbench"
    var initialProperties = [
      "runtimeMode": runtimeMode,
      "rfServiceUuid": rfServiceUuid ?? "",
      "rfLabSuiteFlow": rfLabSuiteFlow ?? "",
      "rfMultiSubscriptionMode": multiSubscriptionMode,
      "rfMultiCycles": multiCycles,
    ]
    if let index = arguments.firstIndex(of: "--blehub-nitro-rf-expected-local-name"),
       arguments.indices.contains(index + 1) {
      initialProperties["rfExpectedLocalName"] = arguments[index + 1]
    }
    if let adapterLaunch {
      initialProperties.merge(adapterLaunch.properties) { _, adapter in adapter }
    }
    if permissionCheck {
      func argument(_ key: String) -> String? {
        guard let index = arguments.firstIndex(of: key), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
      }
      guard let phase = argument("--blehub-nitro-permission-phase"),
            ["inspect", "denied", "authorized"].contains(phase),
            let runId = argument("--blehub-nitro-permission-run-id"),
            UUID(uuidString: runId)?.uuidString.lowercased() == runId else {
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-permission-transition:failed:native:invalid-authorization-launch")
        return false
      }
      let authorization: String
      switch CBManager.authorization {
      case .allowedAlways: authorization = "authorized"
      case .denied: authorization = "denied"
      case .restricted: authorization = "restricted"
      case .notDetermined: authorization = "not_determined"
      @unknown default: authorization = "unknown"
      }
      let identity: [String: Any] = [
        "run_id": runId, "phase": phase, "platform": "ios", "source": "launch",
        "pid": ProcessInfo.processInfo.processIdentifier, "boot_id": hostBootId,
        "bundle_id": Bundle.main.bundleIdentifier ?? "",
        "executable": Bundle.main.executableURL?.absoluteString ?? "",
        "authorization_at_launch": authorization,
      ]
      guard let data = try? JSONSerialization.data(withJSONObject: identity, options: [.sortedKeys]),
            let json = String(data: data, encoding: .utf8) else { return false }
      initialProperties["rfPermissionRunId"] = runId
      initialProperties["rfPermissionPhase"] = phase
      initialProperties["rfPermissionNativeIdentity"] = json
      self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-permission-transition:native:authorization:\(json)")
    }
    let fullRun = runtimeMode == "rf-lab-suite" && arguments.contains("--blehub-nitro-rf-recovery-run-id")
    if runtimeMode == "rf-recovery-ten" || fullRun {
      guard let index = arguments.firstIndex(of: "--blehub-nitro-rf-recovery-run-id"),
            arguments.indices.contains(index + 1),
            UUID(uuidString: arguments[index + 1])?.uuidString.lowercased() == arguments[index + 1],
            let bundleId = Bundle.main.bundleIdentifier,
            let executable = Bundle.main.executableURL?.absoluteString else {
        failRecovery("invalid-launch-metadata")
        return false
      }
      if fullRun {
        guard rfCancellationControl != nil, rfControlIdentity?.session == arguments[index + 1],
              rfLabSuiteFlow == nil else { failRecovery("full-suite-control-session-or-flow"); return false }
        fullRecovery = FullRecoveryBeginGate(runId: arguments[index + 1])
      }
      let identity = RecoveryHostIdentity(run_id: arguments[index + 1],
        pid: ProcessInfo.processInfo.processIdentifier, boot_id: hostBootId,
        bundle_id: bundleId, executable: executable, source: "launch")
      recoveryIdentity = identity
      initialProperties["rfRecoveryRunId"] = identity.run_id
      writeRecoveryRecord("bootstrap", identity: identity)
    }
    if let rfCccDisableRejectControl { initialProperties["rfCccDisableRejectControl"] = rfCccDisableRejectControl }
    if let rfCccEnableRejectControl { initialProperties["rfCccEnableRejectControl"] = rfCccEnableRejectControl }
    if let rfWriteRejectControl { initialProperties["rfWriteRejectControl"] = rfWriteRejectControl }
    if let rfCancellationControl {
      guard runtimeMode == "rf-lab-suite" || runtimeMode == rfControlIdentity?.mode else {
        failRfControl("control-mode-does-not-match-launch"); return false
      }
      initialProperties["rfCancellationControl"] = rfCancellationControl
    }
    pendingReactNativeFactory = factory
    pendingReactNativeProperties = initialProperties
    pendingReactNativeLaunchOptions = launchOptions
    if let window {
      connect(window: window)
    }

    return true
  }

  private func handleRuntimeMarker(_ message: String) {
    if adapterStartup != nil {
      self.writeRuntimeMarker(message, adapterJavaScript: true)
      return
    }
    let writeRejectFailed = rfControlLock.withLock { () -> Bool in
      writeRejectControl?.observe(message)
      cccEnableRejectControl?.observe(message)
      cccDisableRejectControl?.observe(message)
      return (writeRejectControl?.failed == true || cccEnableRejectControl?.failed == true || cccDisableRejectControl?.failed == true) && !rfControlFailed
    }
    if writeRejectFailed { failRfControl("write-reject-runtime-or-readiness") }
    observeRecoveryMarker(message)
    if message.contains("BLEHUB_NITRO_RUNTIME rf-control:listener:") {
      let ready = rfControlLock.withLock { () -> Bool in
        guard !rfControlFailed, !rfControlListenerReady, rfControlNextPhase < rfControlSteps.count,
              message.hasSuffix(rfControlSteps[rfControlNextPhase].listener) else { return false }
        rfControlListenerReady = true
        return true
      }
      if !ready { failRfControl("foreign-early-or-duplicate-listener") }
    }
    if rfControlIdentity != nil, message.contains("BLEHUB_NITRO_RUNTIME rf-control:ready:") {
      let ready = rfControlLock.withLock { () -> Bool in
        guard !rfControlFailed, rfControlListenerReady, rfControlReadyPhase == nil,
              rfControlNextPhase < rfControlSteps.count else { return false }
        let step = rfControlSteps[rfControlNextPhase]
        let identity = step.identity
        let expected = "rf-control:ready:session=\(identity.session):flow=\(identity.flow):mode=\(identity.mode):token=\(identity.token):phase=\(step.request)"
        guard message.hasSuffix(expected) else { return false }
        rfControlReadyPhase = step.phase
        return true
      }
      if !ready { failRfControl("foreign-or-duplicate-readiness") }
    }
    if BleHubPendingReloadHost.enabled {
      // Commit native control readiness BEFORE exposing the marker to the host.
      BleHubPendingReloadHost.observe(message)
      self.writeRuntimeMarker(message)
      return
    }
    self.writeRuntimeMarker(message)
    guard reloadGateEnabled else { return }
    if ProcessInfo.processInfo.arguments.contains("--blehub-nitro-rf-reload-service") {

      guard message.contains("BLEHUB_NITRO_RUNTIME rf-reload:active:") else { return }
      let count = reloadLock.withLock { () -> Int in
        rfReloadActiveCount += 1
        return rfReloadActiveCount
      }
      if count == 1 {
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME rf-reload:native-triggered")
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
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:external-ready")
      } else {
        self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:native-triggered")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
          RCTTriggerReloadCommandListeners("BleHub Nitro reload gate")
        }
      }
    } else if readyCount == 2 {
      self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:complete:passed")
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
    if let adapterStartup {
      adapterStartup.fail("reload-forbidden")
      self.writeRuntimeMarker(AdapterStartupGate.prefix + "failed:native:reload-forbidden")
      return
    }
    if rfControlLock.withLock({ writeRejectControl != nil || cccEnableRejectControl != nil || cccDisableRejectControl != nil }) { failRfControl("reload-forbidden"); return }
    guard externalReloadEnabled else { return }
    removeExternalReloadObserver()
    let ready = reloadLock.withLock { reloadReadyCount == 1 }
    guard ready else {
      self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:notification-before-ready")
      return
    }
    self.writeRuntimeMarker("BLEHUB_NITRO_RUNTIME reload:notification-received")
    RCTTriggerReloadCommandListeners("BleHub host-triggered reload spike")
  }

  deinit {
    for name in rfControlURIs.keys {
      CFNotificationCenterRemoveObserver(
        CFNotificationCenterGetDarwinNotifyCenter(), Unmanaged.passUnretained(self).toOpaque(),
        CFNotificationName(rawValue: name as CFString), nil
      )
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

  @discardableResult
  private func writeRuntimeMarker(_ message: String, adapterJavaScript: Bool = false) -> Bool {
    let committed = adapterStartup?.capture(message, fromJavaScript: adapterJavaScript) ?? true
    // A JS success rejected by the journal/gate must not become a native-captured terminal.
    if committed || !message.hasPrefix(AdapterStartupGate.prefix + "passed:") {
      FileHandle.standardError.write(Data((message + "\n").utf8))
    }
    if !committed {
      let failed = AdapterStartupGate.prefix + "failed:native:" + (adapterStartup?.failure ?? "capture-failed")
      FileHandle.standardError.write(Data((failed + "\n").utf8))
    }
    return committed
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

final class ReactNativeSceneDelegate: UIResponder, UIWindowSceneDelegate {
  var window: UIWindow?

  func scene(
    _ scene: UIScene,
    willConnectTo session: UISceneSession,
    options connectionOptions: UIScene.ConnectionOptions
  ) {
    guard let windowScene = scene as? UIWindowScene,
          let appDelegate = UIApplication.shared.delegate as? AppDelegate else { return }
    let window = UIWindow(windowScene: windowScene)
    self.window = window
    appDelegate.connect(window: window)
  }
}
