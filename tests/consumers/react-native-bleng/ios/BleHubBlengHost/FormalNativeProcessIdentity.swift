import Foundation

// Owned by AppDelegate for the native process lifetime, independently of RN.
// This host-only protocol deliberately has no BLE owner or bridge accessor.
final class FormalNativeProcessIdentity {
  private var responder: NativeProcessIdentityResponder?

  func start(
    arguments: [String], documents: @autoclosure () throws -> URL, context: NativeProcessIdentityContext = .current
  ) throws {
    let flag = "--blehub-formal-identity-run-id"
    let indices = arguments.indices.filter { arguments[$0] == flag }
    guard !arguments.contains(where: { $0.hasPrefix(flag + "=") }) else {
      throw NativeProcessIdentityFiles.InvalidIdentity()
    }
    guard !indices.isEmpty else { return }
    guard indices.count == 1, let index = indices.first, arguments.indices.contains(index + 1),
          NativeProcessIdentityFiles.isCanonicalUuid(arguments[index + 1]) else {
      throw NativeProcessIdentityFiles.InvalidIdentity()
    }
    let runId = arguments[index + 1]
    let base = try documents().resolvingSymlinksInPath().standardizedFileURL
    let family = base.appendingPathComponent("blehub-formal-identity", isDirectory: true)
    let root = family.appendingPathComponent(runId, isDirectory: true)
    if let responder {
      guard responder.matches(root: root, runId: runId) else {
        throw NativeProcessIdentityFiles.InvalidIdentity()
      }
      try NativeProcessIdentityFiles.validate(root)
      return
    }
    try NativeProcessIdentityFiles.prepareDirectory(family)
    try NativeProcessIdentityFiles.prepareDirectory(root)
    let responder = try NativeProcessIdentityResponder(
      root: root, runId: runId, namespace: .formalRN, context: context
    )
    _ = try responder.publish()
    self.responder = responder
  }
}
