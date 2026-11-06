import CoreFoundation
import Foundation
import XCTest

final class NativeProcessIdentityTests: XCTestCase {
  private let runId = "12345678-1234-4234-8234-123456789abc"
  private let bootId = "23456789-2345-4345-8345-23456789abcd"
  private let challenge = "3456789a-3456-4456-8456-3456789abcde"
  private let flag = "--blehub-formal-identity-run-id"

  private func withDocuments(_ body: (URL) throws -> Void) throws {
    let documents = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: documents) }
    try body(documents.resolvingSymlinksInPath())
  }

  private var context: NativeProcessIdentityContext {
    NativeProcessIdentityContext(
      bundleId: "dev.blehub.identity-tests", executable: "/test/NativeHost",
      pid: 123, makeBootId: { self.bootId }
    )
  }

  private func root(_ documents: URL) -> URL {
    documents.appendingPathComponent("blehub-formal-identity/\(runId)", isDirectory: true)
  }

  private func object(_ file: URL) throws -> [String: Any] {
    try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
  }

  func testOptInIsStrictAndOptOutDoesNotCreateIdentityOrFiles() throws {
    try withDocuments { documents in
      var boots = 0
      let context = NativeProcessIdentityContext(
        bundleId: "test", executable: "/test", pid: 1,
        makeBootId: { boots += 1; return self.bootId }
      )
      let host = FormalNativeProcessIdentity()
      func unavailableDocuments() throws -> URL {
        XCTFail("opt-out must not resolve Documents")
        throw NativeProcessIdentityFiles.InvalidIdentity()
      }
      try host.start(arguments: [], documents: try unavailableDocuments(), context: context)
      try host.start(arguments: ["host", "--unrelated"], documents: documents, context: context)
      XCTAssertEqual(boots, 0)
      XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: documents.path), [])
      for arguments in [
        [flag], [flag, "../escape"], [flag, runId.uppercased()], [flag, " \(runId)"],
        [flag, runId, flag, runId], ["\(flag)=\(runId)"], [flag, ""],
      ] {
        XCTAssertThrowsError(try host.start(arguments: arguments, documents: documents, context: context))
      }
      XCTAssertEqual(boots, 0)
      XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: documents.path), [])
    }
  }

  func testBootIsCreatedOnceAndReceiptHasOnlyTheFiveNativeFields() throws {
    try withDocuments { documents in
      var boots = 0
      let context = NativeProcessIdentityContext(
        bundleId: "test.bundle", executable: "/test/host", pid: 456,
        makeBootId: { boots += 1; return self.bootId }
      )
      let host = FormalNativeProcessIdentity()
      try host.start(arguments: [flag, runId], documents: documents, context: context)
      let file = root(documents).appendingPathComponent("native-identity.json")
      let first = try Data(contentsOf: file)
      let response = root(documents).appendingPathComponent("native-inspection.json")
      XCTAssertTrue(try object(response)["challenge"] is NSNull)
      try Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}".utf8).write(
        to: root(documents).appendingPathComponent("identity-request.json"), options: .atomic
      )
      CFNotificationCenterPostNotification(
        CFNotificationCenterGetDarwinNotifyCenter(),
        CFNotificationName("dev.blehub.formal-rn.\(runId).\(bootId).inspect" as CFString), nil, nil, true
      )
      RunLoop.current.run(until: Date().addingTimeInterval(0.15))
      XCTAssertEqual(try object(response)["challenge"] as? String, challenge)
      let answered = try Data(contentsOf: response)
      try host.start(arguments: [flag, runId], documents: documents, context: context)
      XCTAssertEqual(boots, 1)
      XCTAssertEqual(try Data(contentsOf: file), first)
      XCTAssertEqual(try Data(contentsOf: response), answered, "same-instance RN restart must preserve the response")
      let identity = try object(file)
      XCTAssertEqual(Set(identity.keys), ["run_id", "boot_id", "pid", "bundle_id", "executable"])
      XCTAssertEqual(identity["run_id"] as? String, runId)
      XCTAssertEqual(identity["boot_id"] as? String, bootId)
      XCTAssertEqual(identity["pid"] as? Int, 456)
      XCTAssertEqual(identity["bundle_id"] as? String, "test.bundle")
      XCTAssertEqual(identity["executable"] as? String, "/test/host")
      XCTAssertThrowsError(try host.start(arguments: [flag, challenge], documents: documents, context: context))
      XCTAssertEqual(boots, 1)
      withExtendedLifetime(host) {}
    }
  }

  func testFormalSentinelPrecedesIdentityAndRepublishNeverResetsARealResponse() throws {
    try withDocuments { documents in
      var boots = 0
      let context = NativeProcessIdentityContext(
        bundleId: "dev.blehub.identity-tests", executable: "/test/NativeHost", pid: 123,
        makeBootId: { boots += 1; return self.bootId }
      )
      let responder = try NativeProcessIdentityResponder(
        root: documents, runId: runId, namespace: .formalRN, context: context
      )
      let identityFile = documents.appendingPathComponent("native-identity.json")
      let response = documents.appendingPathComponent("native-inspection.json")
      XCTAssertFalse(FileManager.default.fileExists(atPath: identityFile.path))
      enum WriteFailure: Error { case identity }
      var observedBeforeIdentity = false
      XCTAssertThrowsError(try responder.publish(beforeIdentityWrite: {
        XCTAssertFalse(FileManager.default.fileExists(atPath: identityFile.path))
        let ready = try object(response)
        XCTAssertTrue(ready["challenge"] is NSNull)
        XCTAssertEqual((ready["identity"] as? [String: Any])?["boot_id"] as? String, bootId)
        observedBeforeIdentity = true
        throw WriteFailure.identity
      }))
      XCTAssertTrue(observedBeforeIdentity)
      XCTAssertFalse(FileManager.default.fileExists(atPath: identityFile.path))
      let prepared = try Data(contentsOf: response)
      let receipt = try responder.publish(beforeIdentityWrite: {
        XCTAssertFalse(FileManager.default.fileExists(atPath: identityFile.path))
        XCTAssertEqual(try Data(contentsOf: response), prepared)
      })
      let sentinel = try object(response)
      XCTAssertEqual(Set(sentinel.keys), ["identity", "challenge"])
      XCTAssertTrue(sentinel["challenge"] is NSNull)
      XCTAssertNil(sentinel["challenge"] as? String, "a sentinel cannot satisfy a fresh UUID challenge")
      let sentinelIdentity = try XCTUnwrap(sentinel["identity"] as? [String: Any])
      XCTAssertEqual(Set(sentinelIdentity.keys), ["run_id", "boot_id", "pid", "bundle_id", "executable"])
      XCTAssertEqual(sentinelIdentity as NSDictionary, try object(identityFile) as NSDictionary)
      XCTAssertEqual(sentinelIdentity["boot_id"] as? String, bootId)
      let initial = try Data(contentsOf: response)
      XCTAssertEqual(try responder.publish(), receipt)
      XCTAssertEqual(try Data(contentsOf: response), initial)
      try Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}".utf8).write(
        to: documents.appendingPathComponent("identity-request.json"), options: .atomic
      )
      CFNotificationCenterPostNotification(
        CFNotificationCenterGetDarwinNotifyCenter(),
        CFNotificationName("dev.blehub.formal-rn.\(runId).\(bootId).inspect" as CFString), nil, nil, true
      )
      RunLoop.current.run(until: Date().addingTimeInterval(0.15))
      XCTAssertEqual(try object(response)["challenge"] as? String, challenge)
      let answered = try Data(contentsOf: response)
      XCTAssertNotEqual(answered, initial)
      XCTAssertEqual(try responder.publish(), receipt)
      XCTAssertEqual(try Data(contentsOf: response), answered, "republish must not reset to null")
      XCTAssertEqual(boots, 1)
      try Data("malformed after publication".utf8).write(to: response, options: .atomic)
      XCTAssertThrowsError(try responder.publish())
      XCTAssertEqual(try Data(contentsOf: response), Data("malformed after publication".utf8))
      withExtendedLifetime(responder) {}
    }
  }

  func testFormalStartupDoesNotOverwriteMalformedHistoricalInspection() throws {
    let oldIdentity: [String: Any] = [
      "run_id": runId, "boot_id": challenge, "pid": 42,
      "bundle_id": "dev.blehub.identity-tests", "executable": "/test/NativeHost",
    ]
    let originalIdentity = try JSONSerialization.data(withJSONObject: oldIdentity, options: [.sortedKeys])
    let invalidObjects: [[String: Any]] = [
      [:], ["identity": oldIdentity], ["identity": NSNull(), "challenge": NSNull()],
      ["identity": oldIdentity, "challenge": "bad"],
      ["identity": oldIdentity, "challenge": challenge.uppercased()],
      ["identity": oldIdentity, "challenge": NSNull(), "extra": "x"],
      ["identity": oldIdentity.merging(["pid": true]) { _, new in new }, "challenge": NSNull()],
      ["identity": oldIdentity.merging(["pid": -1]) { _, new in new }, "challenge": NSNull()],
      ["identity": oldIdentity.merging(["run_id": bootId]) { _, new in new }, "challenge": NSNull()],
      ["identity": oldIdentity.merging(["executable": "/foreign"]) { _, new in new }, "challenge": NSNull()],
      ["identity": oldIdentity.merging(["extra": "x"]) { _, new in new }, "challenge": NSNull()],
    ]
    let invalid = try invalidObjects.map { try JSONSerialization.data(withJSONObject: $0, options: [.sortedKeys]) }
      + [Data("not JSON".utf8), Data(repeating: 32, count: 4097),
         Data("{\"identity\":{},\"identity\":{},\"challenge\":null}".utf8)]
    for payload in invalid {
      try withDocuments { documents in
        let identityFile = documents.appendingPathComponent("native-identity.json")
        let response = documents.appendingPathComponent("native-inspection.json")
        try originalIdentity.write(to: identityFile, options: .atomic)
        try payload.write(to: response, options: .atomic)
        let responder = try NativeProcessIdentityResponder(
          root: documents, runId: runId, namespace: .formalRN, context: context
        )
        XCTAssertThrowsError(try responder.publish())
        XCTAssertEqual(try Data(contentsOf: response), payload)
        XCTAssertEqual(try Data(contentsOf: identityFile), originalIdentity)
        withExtendedLifetime(responder) {}
      }
    }
    for previousChallenge in [NSNull(), challenge] as [Any] {
      try withDocuments { documents in
        let response = documents.appendingPathComponent("native-inspection.json")
        let previous = try JSONSerialization.data(
          withJSONObject: ["identity": oldIdentity, "challenge": previousChallenge], options: [.sortedKeys]
        )
        try previous.write(to: response, options: .atomic)
        let responder = try NativeProcessIdentityResponder(
          root: documents, runId: runId, namespace: .formalRN, context: context
        )
        _ = try responder.publish()
        let sentinel = try object(response)
        XCTAssertTrue(sentinel["challenge"] is NSNull)
        XCTAssertEqual((sentinel["identity"] as? [String: Any])?["boot_id"] as? String, bootId)
        XCTAssertEqual(
          sentinel["identity"] as? NSDictionary,
          try object(documents.appendingPathComponent("native-identity.json")) as NSDictionary
        )
        XCTAssertNotEqual(try Data(contentsOf: response), previous, "a new native boot replaces only validated history")
        withExtendedLifetime(responder) {}
      }
    }
  }

  func testDarwinNotificationAnswersOnlyFreshBoundedExactRequests() throws {
    try withDocuments { documents in
      let host = FormalNativeProcessIdentity()
      try host.start(arguments: [flag, runId], documents: documents, context: context)
      let directory = root(documents)
      let request = directory.appendingPathComponent("identity-request.json")
      let response = directory.appendingPathComponent("native-inspection.json")
      let initialInspection = try Data(contentsOf: response)
      XCTAssertTrue(try object(response)["challenge"] is NSNull)
      let name = "dev.blehub.formal-rn.\(runId).\(bootId).inspect"
      func post(_ payload: Data, namespace: String = name) throws {
        try payload.write(to: request, options: .atomic)
        CFNotificationCenterPostNotification(
          CFNotificationCenterGetDarwinNotifyCenter(), CFNotificationName(namespace as CFString), nil, nil, true
        )
        RunLoop.current.run(until: Date().addingTimeInterval(0.15))
      }
      let valid = Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}".utf8)
      for invalid in [
        Data("{}".utf8), Data("[]".utf8), Data("not json".utf8),
        Data("{\"boot_id\":\"\(runId)\",\"challenge\":\"\(challenge)\"}".utf8),
        Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"../escape\"}".utf8),
        Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge.uppercased())\"}".utf8),
        Data("{\"boot_id\":\"\(bootId)\",\"challenge\":123}".utf8),
        Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\",\"extra\":\"x\"}".utf8),
        valid + Data(repeating: 32, count: 1025 - valid.count),
      ] {
        try post(invalid)
        XCTAssertEqual(try Data(contentsOf: response), initialInspection)
      }
      try post(valid, namespace: "dev.blehub.native-hil.\(runId).\(bootId).inspect")
      XCTAssertEqual(try Data(contentsOf: response), initialInspection)
      try post(valid + Data(repeating: 32, count: 1024 - valid.count))
      let reply = try object(response)
      XCTAssertEqual(Set(reply.keys), ["identity", "challenge"])
      XCTAssertEqual(reply["challenge"] as? String, challenge)
      XCTAssertEqual(
        reply["identity"] as? NSDictionary,
        try object(directory.appendingPathComponent("native-identity.json")) as NSDictionary
      )
      let accepted = try Data(contentsOf: response)
      try post(Data("{}".utf8))
      XCTAssertEqual(try Data(contentsOf: response), accepted, "invalid request must not replace a prior reply")
      withExtendedLifetime(host) {}
    }
  }

  func testRequestRejectsDuplicateEscapedNestedAndNoncanonicalOrdering() throws {
    try withDocuments { documents in
      let host = FormalNativeProcessIdentity()
      try host.start(arguments: [flag, runId], documents: documents, context: context)
      let request = root(documents).appendingPathComponent("identity-request.json")
      let response = root(documents).appendingPathComponent("native-inspection.json")
      let initialInspection = try Data(contentsOf: response)
      let invalid = [
        ("same boot key and value", "{\"boot_id\":\"\(bootId)\",\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}"),
        ("different boot values, valid last", "{\"boot_id\":\"\(runId)\",\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}"),
        ("different boot values, valid first", "{\"boot_id\":\"\(bootId)\",\"boot_id\":\"\(runId)\",\"challenge\":\"\(challenge)\"}"),
        ("same challenge key and value", "{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\",\"challenge\":\"\(challenge)\"}"),
        ("different challenge values", "{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(runId)\",\"challenge\":\"\(challenge)\"}"),
        ("both keys repeated", "{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\",\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}"),
        ("escaped boot key", "{\"boot_\\u0069d\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}"),
        ("escaped challenge key", "{\"boot_id\":\"\(bootId)\",\"cha\\u006clenge\":\"\(challenge)\"}"),
        ("escaped duplicate key", "{\"boot_id\":\"\(bootId)\",\"boot_\\u0069d\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}"),
        ("escaped boot UUID", "{\"boot_id\":\"\\u0032\(bootId.dropFirst())\",\"challenge\":\"\(challenge)\"}"),
        ("escaped challenge UUID", "{\"boot_id\":\"\(bootId)\",\"challenge\":\"\\u0033\(challenge.dropFirst())\"}"),
        ("nested unknown key", "{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\",\"extra\":{\"boot_id\":\"\(bootId)\"}}"),
        ("nested challenge value", "{\"boot_id\":\"\(bootId)\",\"challenge\":{\"challenge\":\"\(challenge)\"}}"),
        ("reverse canonical key order", "{\"challenge\":\"\(challenge)\",\"boot_id\":\"\(bootId)\"}"),
        ("reverse order with whitespace", "{ \"challenge\": \"\(challenge)\", \"boot_id\": \"\(bootId)\" }"),
        ("whitespace inside UUID", "{\"boot_id\":\"\(bootId)\",\"challenge\":\" \(challenge)\"}"),
        ("whitespace inside key", "{\"boot_id\":\"\(bootId)\",\"chal lenge\":\"\(challenge)\"}"),
        ("non-ASCII whitespace", "\u{00a0}{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}"),
      ]
      for (label, payload) in invalid {
        try Data(payload.utf8).write(to: request, options: .atomic)
        CFNotificationCenterPostNotification(
          CFNotificationCenterGetDarwinNotifyCenter(),
          CFNotificationName("dev.blehub.formal-rn.\(runId).\(bootId).inspect" as CFString), nil, nil, true
        )
        RunLoop.current.run(until: Date().addingTimeInterval(0.15))
        XCTAssertEqual(try Data(contentsOf: response), initialInspection, label)
      }
      withExtendedLifetime(host) {}
    }
  }

  func testCanonicalRequestAcceptsLegacyJsonDumpsSpacesCompactAndAsciiPadding() throws {
    for namespace in [NativeProcessIdentityResponder.Namespace.formalRN, .nativeHil] {
      try withDocuments { documents in
        let responder = try NativeProcessIdentityResponder(
          root: documents, runId: runId, namespace: namespace, context: context
        )
        _ = try responder.publish()
        let compact = "{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}"
        let padded = Data((" \t\n\r" + compact).utf8)
        let padding = Array(repeating: [UInt8]([0x20, 0x09, 0x0a, 0x0d]), count: (1024 - padded.count) / 4).flatMap { $0 }
        let boundary = padded + Data(padding) + Data(repeating: 0x20, count: (1024 - padded.count) % 4)
        XCTAssertEqual(boundary.count, 1024)
        let payloads = [
          Data(compact.utf8),
          Data("{\"boot_id\": \"\(bootId)\", \"challenge\": \"\(challenge)\"}".utf8),
          Data("\r\n{\n\t\"boot_id\" : \"\(bootId)\" ,\n\t\"challenge\" : \"\(challenge)\"\n}\r\n".utf8),
          boundary,
        ]
        let response = documents.appendingPathComponent("native-inspection.json")
        for payload in payloads {
          if FileManager.default.fileExists(atPath: response.path) {
            try FileManager.default.removeItem(at: response)
          }
          try payload.write(to: documents.appendingPathComponent("identity-request.json"), options: .atomic)
          CFNotificationCenterPostNotification(
            CFNotificationCenterGetDarwinNotifyCenter(),
            CFNotificationName("\(namespace.rawValue).\(runId).\(bootId).inspect" as CFString), nil, nil, true
          )
          RunLoop.current.run(until: Date().addingTimeInterval(0.15))
          XCTAssertEqual(try object(response)["challenge"] as? String, challenge)
        }
        withExtendedLifetime(responder) {}
      }
    }
  }

  func testSymlinkDirectoriesAndFilesCannotEscapeRunRoot() throws {
    try withDocuments { documents in
      let outside = documents.appendingPathComponent("outside", isDirectory: true)
      try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: false)
      let family = documents.appendingPathComponent("blehub-formal-identity", isDirectory: true)
      try FileManager.default.createSymbolicLink(at: family, withDestinationURL: outside)
      XCTAssertThrowsError(try FormalNativeProcessIdentity().start(
        arguments: [flag, runId], documents: documents, context: context
      ))
      XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: outside.path), [])
      try FileManager.default.removeItem(at: family)
      try FileManager.default.createDirectory(at: family, withIntermediateDirectories: false)
      try FileManager.default.createSymbolicLink(at: root(documents), withDestinationURL: outside)
      XCTAssertThrowsError(try FormalNativeProcessIdentity().start(
        arguments: [flag, runId], documents: documents, context: context
      ))
      try FileManager.default.removeItem(at: root(documents))
      for filename in ["native-identity.json", "identity-request.json", "native-inspection.json"] {
        try FileManager.default.createDirectory(at: root(documents), withIntermediateDirectories: false)
        let sentinel = outside.appendingPathComponent(filename)
        try Data("untouched".utf8).write(to: sentinel)
        try FileManager.default.createSymbolicLink(
          at: root(documents).appendingPathComponent(filename), withDestinationURL: sentinel
        )
        XCTAssertThrowsError(try FormalNativeProcessIdentity().start(
          arguments: [flag, runId], documents: documents, context: context
        ))
        XCTAssertEqual(try Data(contentsOf: sentinel), Data("untouched".utf8))
        try FileManager.default.removeItem(at: root(documents))
      }
    }
  }

  func testLiveResponderRefusesReplacedPathsAndNonRegularRequests() throws {
    try withDocuments { documents in
      let host = FormalNativeProcessIdentity()
      try host.start(arguments: [flag, runId], documents: documents, context: context)
      let directory = root(documents)
      let request = directory.appendingPathComponent("identity-request.json")
      let response = directory.appendingPathComponent("native-inspection.json")
      let initialInspection = try Data(contentsOf: response)
      let outside = documents.appendingPathComponent("outside", isDirectory: true)
      try FileManager.default.createDirectory(at: outside, withIntermediateDirectories: false)
      let externalRequest = outside.appendingPathComponent("request.json")
      let valid = Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}".utf8)
      try valid.write(to: externalRequest)
      func post() {
        CFNotificationCenterPostNotification(
          CFNotificationCenterGetDarwinNotifyCenter(),
          CFNotificationName("dev.blehub.formal-rn.\(runId).\(bootId).inspect" as CFString), nil, nil, true
        )
        RunLoop.current.run(until: Date().addingTimeInterval(0.15))
      }
      try FileManager.default.createSymbolicLink(at: request, withDestinationURL: externalRequest)
      post()
      XCTAssertEqual(try Data(contentsOf: response), initialInspection)
      try FileManager.default.removeItem(at: request)
      try FileManager.default.linkItem(at: externalRequest, to: request)
      post()
      XCTAssertEqual(try Data(contentsOf: response), initialInspection)
      try FileManager.default.removeItem(at: request)
      try FileManager.default.createDirectory(at: request, withIntermediateDirectories: false)
      post()
      XCTAssertEqual(try Data(contentsOf: response), initialInspection)
      try FileManager.default.removeItem(at: request)
      try valid.write(to: request, options: .atomic)
      let sentinel = outside.appendingPathComponent("native-inspection.json")
      try Data("untouched".utf8).write(to: sentinel)
      try FileManager.default.removeItem(at: response)
      try FileManager.default.createSymbolicLink(at: response, withDestinationURL: sentinel)
      post()
      XCTAssertEqual(try Data(contentsOf: sentinel), Data("untouched".utf8))
      try FileManager.default.removeItem(at: response)
      try FileManager.default.removeItem(at: sentinel)
      try FileManager.default.moveItem(at: directory, to: directory.appendingPathExtension("retired"))
      try FileManager.default.createSymbolicLink(at: directory, withDestinationURL: outside)
      try valid.write(to: outside.appendingPathComponent("identity-request.json"))
      post()
      XCTAssertFalse(FileManager.default.fileExists(atPath: sentinel.path))
      withExtendedLifetime(host) {}
    }
  }

  func testResponderIsRetiredAndANewNativeLifetimeDoesNotAnswerTheOldBoot() throws {
    try withDocuments { documents in
      var host: FormalNativeProcessIdentity? = FormalNativeProcessIdentity()
      try host?.start(arguments: [flag, runId], documents: documents, context: context)
      host = nil
      let request = root(documents).appendingPathComponent("identity-request.json")
      let response = root(documents).appendingPathComponent("native-inspection.json")
      let oldRequest = Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}".utf8)
      try oldRequest.write(to: request, options: .atomic)
      func post(_ boot: String) {
        CFNotificationCenterPostNotification(
          CFNotificationCenterGetDarwinNotifyCenter(),
          CFNotificationName("dev.blehub.formal-rn.\(runId).\(boot).inspect" as CFString), nil, nil, true
        )
        RunLoop.current.run(until: Date().addingTimeInterval(0.15))
      }
      let retiredInspection = try Data(contentsOf: response)
      post(bootId)
      XCTAssertEqual(try Data(contentsOf: response), retiredInspection)
      XCTAssertTrue(try object(response)["challenge"] is NSNull, "a retired boot's sentinel is not live proof")
      let newContext = NativeProcessIdentityContext(
        bundleId: context.bundleId, executable: context.executable, pid: context.pid, makeBootId: { self.challenge }
      )
      let nextHost = FormalNativeProcessIdentity()
      try nextHost.start(arguments: [flag, runId], documents: documents, context: newContext)
      XCTAssertEqual(try object(root(documents).appendingPathComponent("native-identity.json"))["boot_id"] as? String, challenge)
      let nextInspection = try Data(contentsOf: response)
      XCTAssertTrue(try object(response)["challenge"] is NSNull)
      XCTAssertNotEqual(nextInspection, retiredInspection)
      post(bootId)
      post(challenge)
      XCTAssertEqual(try Data(contentsOf: response), nextInspection)
      try Data("{\"boot_id\":\"\(challenge)\",\"challenge\":\"\(runId)\"}".utf8).write(to: request, options: .atomic)
      post(challenge)
      XCTAssertEqual(try object(response)["challenge"] as? String, runId)
      withExtendedLifetime(nextHost) {}
    }
  }

  func testLegacyNamespaceAndReceiptRemainCompatible() throws {
    try withDocuments { documents in
      let responder = try NativeProcessIdentityResponder(
        root: documents, runId: runId, namespace: .nativeHil, context: context
      )
      XCTAssertFalse(FileManager.default.fileExists(atPath: documents.appendingPathComponent("native-inspection.json").path))
      let receipt = try responder.publish()
      XCTAssertTrue(receipt.hasPrefix("apple_native_hil_identity="))
      _ = try responder.publish()
      XCTAssertFalse(FileManager.default.fileExists(atPath: documents.appendingPathComponent("native-inspection.json").path))
      let data = Data("{\"boot_id\":\"\(bootId)\",\"challenge\":\"\(challenge)\"}".utf8)
      try data.write(to: documents.appendingPathComponent("identity-request.json"), options: .atomic)
      CFNotificationCenterPostNotification(
        CFNotificationCenterGetDarwinNotifyCenter(),
        CFNotificationName("dev.blehub.native-hil.\(runId).\(bootId).inspect" as CFString), nil, nil, true
      )
      RunLoop.current.run(until: Date().addingTimeInterval(0.2))
      XCTAssertEqual(try object(documents.appendingPathComponent("native-inspection.json"))["challenge"] as? String, challenge)
      withExtendedLifetime(responder) {}
    }
  }
}
