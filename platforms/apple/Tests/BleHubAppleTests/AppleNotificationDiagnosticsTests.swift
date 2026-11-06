import Foundation
import XCTest
@testable import BleHubApple

final class AppleNotificationDiagnosticsTests: XCTestCase {
    private func row(_ data: Data) throws -> [String: Any] {
        XCTAssertLessThanOrEqual(data.count, 3000)
        let prefix = Data(AppleNotificationDiagnostics.prefix.utf8)
        XCTAssertTrue(data.starts(with: prefix))
        XCTAssertEqual(data.last, 0x0a)
        XCTAssertFalse(String(decoding: data, as: UTF8.self).contains("BLEHUB_NITRO_RUNTIME"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(data.dropFirst(prefix.count))) as? [String: Any])
    }

    func testDisabledDiagnosticsDoNotEvaluateFieldsOrWrite() {
        var writes = 0
        var evaluations = 0
        let recorder = AppleNotificationDiagnostics(enabled: false) { _ in writes += 1 }
        func fields() -> [String: Any] { evaluations += 1; return [:] }
        for _ in 0..<256 { recorder.record("value.entry", fields: fields()) }
        XCTAssertEqual(writes, 0)
        XCTAssertEqual(evaluations, 0)
        let normal = AppleNotificationDiagnostics(write: { _ in writes += 1 })
        XCTAssertEqual(normal.isEnabled, ProcessInfo.processInfo.environment["BLEHUB_APPLE_NOTIFICATION_DIAGNOSTICS"] == "1")
    }

    func testOverflowIsExplicitWithin128RecordOwnerBudgetAndStopsEvaluatingFields() throws {
        var output: [Data] = []
        var evaluations = 0
        let recorder = AppleNotificationDiagnostics(enabled: true) { output.append($0) }
        func fields() -> [String: Any] { evaluations += 1; return ["subscriptionId": "34009"] }
        for _ in 0..<1000 { recorder.record("value.entry", fields: fields()) }
        XCTAssertEqual(output.count, 128)
        XCTAssertEqual(evaluations, 127)
        let rows = try output.map(row)
        XCTAssertEqual(Set(rows.compactMap { $0["owner"] as? String }).count, 1)
        XCTAssertEqual(rows.compactMap { $0["sequence"] as? Int }, Array(1...128))
        XCTAssertEqual(rows.last?["event"] as? String, "overflow")
        XCTAssertEqual(rows.last?["suppressedEvent"] as? String, "value.entry")
        XCTAssertEqual(rows.last?["droppedAtLeast"] as? Int, 1)
        XCTAssertEqual(rows.last?["recordLimit"] as? Int, 128)
    }

    func testUnicodeEscapesAndWholeRecordTruncationRemainBoundedValidJson() throws {
        var output: [Data] = []
        let recorder = AppleNotificationDiagnostics(enabled: true) { output.append($0) }
        recorder.record("notification-state.entry", fields: ["errorDomain": String(repeating: "界🙂\n\"", count: 1000)])
        let text = try row(XCTUnwrap(output.first))
        XCTAssertEqual(text["truncated"] as? Bool, true)
        XCTAssertEqual(text["truncatedFields"] as? [String], ["errorDomain"])
        let fields = try XCTUnwrap(text["fields"] as? [String: Any])
        XCTAssertLessThanOrEqual(try XCTUnwrap(fields["errorDomain"] as? String).utf8.count, 160)
        recorder.record("value.entry", fields: Dictionary(uniqueKeysWithValues: (0..<48).map {
            ("field\($0)", String(repeating: "\u{01}", count: 160))
        }))
        let fallback = try row(XCTUnwrap(output.last))
        XCTAssertEqual(fallback["truncated"] as? Bool, true)
        XCTAssertEqual(fallback["truncationReason"] as? String, "record-byte-limit")
        XCTAssertGreaterThan(try XCTUnwrap(fallback["originalBytes"] as? Int), 3000)
        XCTAssertNil(fallback["fields"])
        recorder.record("value.disposition", fields: ["disposition": "notification"])
        let next = try row(XCTUnwrap(output.last))
        XCTAssertEqual(next["sequence"] as? Int, 3)
        XCTAssertEqual(next["event"] as? String, "value.disposition")
    }
}
