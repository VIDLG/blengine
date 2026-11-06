import BleHubUniFfi
import Foundation
import XCTest

final class SystemLossWitnessTests: XCTestCase {
    func testOneShotBusyOrOtherAdmissionFailureNeverAcceptsSiblingOrExportsPending() async throws {
        for scenario in SystemLossCase.allCases {
            for failure in [LossAdmissionFailure.busy, .notReady] {
                let f = try LossFixture(scenario)
                defer { try? f.hub.dispose() }
                let control = try f.control(f.prepare())
                let base = try f.ledger.bindControl(control)
                try f.ledger.observeArmed(f.physical(control: control, base: base))
                let attempt = SystemLossOneShotAttempt(witness: f.ledger)
                defer { attempt.retireWaiters() }
                var headSubmissions = 0, siblingSubmissions = 0, exports = 0, samples = 0
                do {
                    _ = try await attempt.run(deadline: ContinuousClock.now.advanced(by: .seconds(2)),
                        beginHead: {
                            headSubmissions += 1
                            try f.dispatchRole(head: true)
                            return f.pendingOperation(f.head)
                        }, beginSibling: {
                            siblingSubmissions += 1
                            // Simulated actor rejection at wait(), BEFORE the real
                            // BleBackend boundary. No sibling delegate is called.
                            return f.pendingOperation(f.sibling, failure: failure)
                        }, check: {}, snapshot: {
                            samples += 1
                            return f.lane()
                        }, publish: { _ in exports += 1 })
                    XCTFail("actor rejection must terminate the one-shot attempt")
                } catch let error as SystemLossAttemptOperationError {
                    XCTAssertEqual(error.role, "sibling")
                    XCTAssertEqual(error.commandId, f.sibling)
                    XCTAssertEqual(error.underlying as? LossAdmissionFailure, failure)
                }
                XCTAssertEqual(attempt.state, "failed")
                XCTAssertEqual(headSubmissions, 1)
                XCTAssertEqual(siblingSubmissions, 1)
                XCTAssertEqual(samples, 0)
                XCTAssertEqual(exports, 0)
                XCTAssertTrue(f.ledger.successfulCommandIds().contains(f.head))
                XCTAssertFalse(f.ledger.successfulCommandIds().contains(f.sibling))
                XCTAssertFalse(f.backend.dispatched.contains(f.sibling))
                XCTAssertEqual(try f.ledger.witness(), base)
                do {
                    _ = try await attempt.run(deadline: ContinuousClock.now.advanced(by: .seconds(2)),
                        beginHead: { headSubmissions += 1; return f.pendingOperation(f.head) },
                        beginSibling: { siblingSubmissions += 1; return f.pendingOperation(f.sibling) },
                        check: {}, snapshot: { f.lane() }, publish: { _ in exports += 1 })
                    XCTFail("a failed attempt cannot retry")
                } catch {}
                XCTAssertEqual(headSubmissions, 1)
                XCTAssertEqual(siblingSubmissions, 1)
                XCTAssertEqual(exports, 0)
            }
        }
    }

    func testOneShotAcceptedPathExportsOnlyActualBackendAndFreshNativePending() async throws {
        for scenario in SystemLossCase.allCases {
            let f = try LossFixture(scenario)
            defer { try? f.hub.dispose() }
            let control = try f.control(f.prepare())
            let base = try f.ledger.bindControl(control)
            try f.ledger.observeArmed(f.physical(control: control, base: base))
            let attempt = SystemLossOneShotAttempt(witness: f.ledger)
            defer { attempt.retireWaiters() }
            var headSubmissions = 0, siblingSubmissions = 0
            var exported: [Data] = []
            let pending = try await attempt.run(deadline: ContinuousClock.now.advanced(by: .seconds(2)),
                beginHead: {
                    headSubmissions += 1
                    try f.dispatchRole(head: true)
                    return f.pendingOperation(f.head)
                }, beginSibling: {
                    XCTAssertTrue(f.ledger.successfulCommandIds().contains(f.head))
                    siblingSubmissions += 1
                    try f.dispatchRole(head: false)
                    return f.pendingOperation(f.sibling)
                }, check: {}, snapshot: {
                    XCTAssertTrue(f.ledger.successfulCommandIds().contains(f.sibling))
                    return f.lane()
                }, publish: { exported.append($0) })
            XCTAssertEqual(attempt.state, "pending-observed")
            XCTAssertEqual(headSubmissions, 1)
            XCTAssertEqual(siblingSubmissions, 1)
            XCTAssertEqual(exported, [pending])
            XCTAssertEqual(try f.ledger.witness(), pending)
            XCTAssertEqual(try rows(pending).count, scenario == .writeEnable ? 4 : 6)
            attempt.stop(failure: nil)
            XCTAssertEqual(attempt.state, "stopped")
            XCTAssertEqual(try f.ledger.witness(), pending)
        }
    }

    func testProviderHeldMustBindActualPendingWitnessSessionControlAndArmedTicket() throws {
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        let control = try f.control(f.prepare())
        let base = try f.ledger.bindControl(control)
        try f.ledger.observeArmed(f.physical(control: control, base: base))
        try f.dispatch()
        let pending = try f.ledger.pending(snapshot: f.lane(), head: f.head, sibling: f.sibling)
        let held = try f.physicalHeld(control: control, pending: pending)
        for drift in ["session_id", "control_sha256", "state", "binding_sha256", "ticket", "witness", "wire"] {
            var object = try SystemLossJSON.object(held)
            if drift == "ticket" || drift == "witness" || drift == "wire" {
                var boundaries = try XCTUnwrap(object["boundaries"] as? [[String: Any]])
                if drift == "witness" { boundaries[0]["witness"] = try SystemLossJSON.object(base) }
                else {
                    var status = try XCTUnwrap(boundaries[0]["status"] as? [String: Any])
                    status[drift == "wire" ? "status_wire" : "ticket"] = drift == "wire" ? "0100" : [:]
                    boundaries[0]["status"] = status
                }
                object["boundaries"] = boundaries
            } else { object[drift] = "stale" }
            XCTAssertThrowsError(try f.ledger.observeHeldPending(SystemLossJSON.canonical(object)), drift)
        }
        try f.ledger.observeHeldPending(held)
        XCTAssertEqual(try f.ledger.witness(), pending)
    }

    func testLossAuthorizationIsAfterPendingAndDoesNotInventTerminals() async throws {
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        let control = try f.control(f.prepare())
        let base = try f.ledger.bindControl(control)
        try f.ledger.observeArmed(f.physical(control: control, base: base))
        let attempt = SystemLossOneShotAttempt(witness: f.ledger)
        defer { attempt.retireWaiters() }
        XCTAssertThrowsError(try attempt.authorizeLoss())
        _ = try await attempt.run(deadline: ContinuousClock.now.advanced(by: .seconds(2)),
            beginHead: { try f.dispatchRole(head: true); return f.pendingOperation(f.head) },
            beginSibling: { try f.dispatchRole(head: false); return f.pendingOperation(f.sibling) },
            check: {}, snapshot: { f.lane() }, publish: { _ in })
        XCTAssertNoThrow(try attempt.authorizeLoss())
        XCTAssertThrowsError(try attempt.authorizeLoss())
        XCTAssertThrowsError(try attempt.terminalResults())
        XCTAssertEqual(attempt.status()["loss_authorized"] as? Bool, true)
    }

    func testOneShotAbsoluteDeadlineDoesNotRestartForNativeReadiness() async throws {
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        let control = try f.control(f.prepare())
        let base = try f.ledger.bindControl(control)
        try f.ledger.observeArmed(f.physical(control: control, base: base))
        let attempt = SystemLossOneShotAttempt(witness: f.ledger)
        defer { attempt.retireWaiters() }
        var exports = 0, siblingSubmissions = 0
        let start = ContinuousClock.now
        do {
            _ = try await attempt.run(deadline: start.advanced(by: .milliseconds(60)),
                beginHead: { try f.dispatchRole(head: true); return f.pendingOperation(f.head) },
                beginSibling: {
                    siblingSubmissions += 1
                    try f.dispatchRole(head: false)
                    return f.pendingOperation(f.sibling)
                }, check: {}, snapshot: { f.lane(state: .awaitingDrive) }, publish: { _ in exports += 1 })
            XCTFail("awaitingDrive must not become pending or extend the deadline")
        } catch {}
        XCTAssertEqual(siblingSubmissions, 1)
        XCTAssertEqual(exports, 0)
        XCTAssertEqual(attempt.state, "failed")
        XCTAssertLessThan(start.duration(to: .now), .seconds(1))
        XCTAssertEqual(try f.ledger.witness(), base)
    }

    func testBothCasesExportExactCumulativeNativeEvidenceAndOptionalFixtures() throws {
        for scenario in SystemLossCase.allCases {
            let f = try LossFixture(scenario)
            defer { try? f.hub.dispose() }
            let setup = try f.prepare()
            let control = try f.control(setup)
            let base = try f.ledger.bindControl(control)
            XCTAssertEqual(try rows(base).count, scenario == .writeEnable ? 1 : 3)
            XCTAssertThrowsError(try f.ledger.pending(
                snapshot: f.lane(), head: f.head, sibling: f.sibling
            ))
            try f.ledger.observeArmed(f.physical(control: control, base: base))
            try f.dispatch()
            let pending = try f.ledger.pending(snapshot: f.lane(), head: f.head, sibling: f.sibling)
            let events = try rows(pending)
            XCTAssertEqual(events.count, scenario == .writeEnable ? 4 : 6)
            XCTAssertEqual(try SystemLossJSON.canonical(Array(events.prefix(events.count - 3))),
                           try SystemLossJSON.canonical(rows(base)))
            let head = events[events.count - 3]
            let sibling = events[events.count - 2]
            XCTAssertEqual(head["command_id"] as? String, String(f.head.value))
            XCTAssertEqual(sibling["command_id"] as? String, String(f.sibling.value))
            let write = scenario == .writeEnable ? head : sibling
            XCTAssertTrue(write["subscription_id"] is NSNull)
            XCTAssertTrue(write["descriptor_id"] is NSNull)
            XCTAssertEqual(write["requested_hex"] as? String, "1020")
            XCTAssertEqual(head["source"] as? String, "backend-dispatch-return")
            XCTAssertEqual(events.last?["source"] as? String, "native-io-lane")
            XCTAssertEqual(try SystemLossJSON.object(pending)["control_sha256"] as? String,
                           try SystemLossJSON.digest(control))
            XCTAssertThrowsError(try f.ledger.pending(snapshot: f.lane(), head: f.head, sibling: f.sibling))
            XCTAssertEqual(try f.ledger.witness(), pending)
            if let path = ProcessInfo.processInfo.environment["BLEHUB_SYSTEM_LOSS_FIXTURE_EXPORT"] {
                let directory = URL(fileURLWithPath: path).appendingPathComponent(scenario.rawValue)
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                for (name, data) in [("setup", setup), ("control", control), ("base", base), ("pending", pending)] {
                    try data.write(to: directory.appendingPathComponent(name + ".json"), options: .atomic)
                }
            }
        }
    }

    func testCallbacksBeforeReturnAreRetainedButAreNotAcceptance() throws {
        let f = try LossFixture(.disableWrite)
        defer { try? f.hub.dispose() }
        f.backend.beforeReturn = { command in
            XCTAssertFalse(f.ledger.successfulCommandIds().contains(command))
        }
        _ = try f.prepare()
        XCTAssertTrue(f.ledger.successfulCommandIds().contains(f.baselineCommand))
    }

    func testRejectedDelegateNeverRecordsAcceptanceEvenWithEarlyCallback() throws {
        for scenario in SystemLossCase.allCases {
            let f = try LossFixture(scenario)
            defer { try? f.hub.dispose() }
            let setup = try f.prepare()
            let control = try f.control(setup)
            let base = try f.ledger.bindControl(control)
            try f.ledger.observeArmed(f.physical(control: control, base: base))
            f.backend.reject = true
            XCTAssertThrowsError(try f.dispatch())
            XCTAssertFalse(f.ledger.successfulCommandIds().contains(f.head))
            XCTAssertThrowsError(try f.ledger.pending(snapshot: f.lane(), head: f.head, sibling: f.sibling))
            XCTAssertEqual(try f.ledger.witness(), base)
        }
    }

    func testRejectedInitializationCannotEstablishIdentity() throws {
        let witness = SystemLossWitness()
        let delegate = LossBackend()
        delegate.reject = true
        let wrapper = SystemLossWitnessBackend(delegate: delegate, witness: witness)
        let hub = try BleHub(backend: wrapper, downstream: LossSink(witness))
        defer { try? hub.dispose() }
        let command = CommandId(value: 18446744073709551615)
        delegate.beforeReturn = { id in XCTAssertFalse(witness.successfulCommandIds().contains(id)) }
        XCTAssertThrowsError(try hub.initialize(command: InitializeCommand(commandId: command)))
        XCTAssertTrue(witness.successfulCommandIds().isEmpty)
        XCTAssertThrowsError(try hub.initialize(command: InitializeCommand(commandId: CommandId(value: 123))))
        XCTAssertEqual(delegate.initializeCalls, 1)
    }

    func testNativeLaneMustBeExactCallbackOwnedFifoNotCapacityDriveOrInternalHead() throws {
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        let control = try f.control(f.prepare())
        let base = try f.ledger.bindControl(control)
        try f.ledger.observeArmed(f.physical(control: control, base: base))
        try f.dispatch()
        for state: AppleGattIoHeadState in [.idle, .awaitingDrive, .waitingForWriteCapacity, .submittingWriteWithoutResponse] {
            XCTAssertThrowsError(try f.ledger.pending(snapshot: f.lane(state: state), head: f.head, sibling: f.sibling))
        }
        for snapshot in [f.lane(connection: 99), f.lane(head: 99), f.lane(queued: []),
                         f.lane(queued: [99]), f.lane(queued: [f.sibling.value, 99]),
                         f.lane(kind: .retireSubscription), f.lane(kind: nil)] {
            XCTAssertThrowsError(try f.ledger.pending(snapshot: snapshot, head: f.head, sibling: f.sibling))
        }
        XCTAssertThrowsError(try f.ledger.pending(snapshot: nil, head: f.head, sibling: f.sibling))
        XCTAssertThrowsError(try f.ledger.pending(snapshot: f.lane(), head: f.sibling, sibling: f.head))
        XCTAssertEqual(try f.ledger.witness(), base)
    }

    func testCompletedNativeCommandsCannotBeRecastAsPendingByStaleSnapshot() throws {
        for scenario in SystemLossCase.allCases {
            let f = try LossFixture(scenario)
            defer { try? f.hub.dispose() }
            let control = try f.control(f.prepare())
            let base = try f.ledger.bindControl(control)
            try f.ledger.observeArmed(f.physical(control: control, base: base))
            try f.dispatch()
            f.backend.emit(.commandCompleted(commandId: f.head, error: nil))
            XCTAssertThrowsError(try f.ledger.pending(snapshot: f.lane(), head: f.head, sibling: f.sibling))
            XCTAssertEqual(try f.ledger.witness(), base)
        }
    }

    func testWrongCasePayloadRouteSubscriptionAndPredictedIdsCannotBecomePending() throws {
        for (scenario, drift) in SystemLossCase.allCases.flatMap({ scenario in
            ["case", "payload", "session", "characteristic", "subscription", "write-type"].map { (scenario, $0) }
        }) {
            let f = try LossFixture(scenario)
            defer { try? f.hub.dispose() }
            let control = try f.control(f.prepare())
            let base = try f.ledger.bindControl(control)
            try f.ledger.observeArmed(f.physical(control: control, base: base))
            try f.dispatch(drift: drift)
            XCTAssertThrowsError(try f.ledger.pending(snapshot: f.lane(), head: f.head, sibling: f.sibling), drift)
            XCTAssertEqual(try f.ledger.witness(), base)
        }
    }

    func testControlIsStrictFrozenAndBindsNativeIdentityNotFutureIds() throws {
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        let setup = try f.prepare()
        let original = try SystemLossJSON.object(f.control(setup))
        for key in ["case", "session_id", "identity", "route", "baseline_command_id", "write_requested", "token", "schema_version"] {
            var changed = original
            changed[key] = key == "schema_version" ? true : "wrong"
            XCTAssertThrowsError(try f.ledger.bindControl(SystemLossJSON.canonical(changed)), key)
        }
        var extra = original
        extra["head_command_id"] = "123"
        XCTAssertThrowsError(try f.ledger.bindControl(SystemLossJSON.canonical(extra)))
        let control = try SystemLossJSON.canonical(original)
        let floatVersion = String(decoding: control, as: UTF8.self).replacingOccurrences(of: "\"schema_version\":1", with: "\"schema_version\":1.0")
        XCTAssertThrowsError(try f.ledger.bindControl(Data(floatVersion.utf8)))
        let base = try f.ledger.bindControl(control)
        XCTAssertThrowsError(try f.ledger.bindControl(control))
        var physical = try SystemLossJSON.object(f.physical(control: control, base: base))
        for key in ["control_sha256", "session_id", "state", "scope"] {
            var changed = physical
            changed[key] = "stale"
            XCTAssertThrowsError(try f.ledger.observeArmed(SystemLossJSON.canonical(changed)), key)
        }
        physical["radio_authorized"] = true
        XCTAssertThrowsError(try f.ledger.observeArmed(SystemLossJSON.canonical(physical)))
        try f.ledger.observeArmed(f.physical(control: control, base: base))
        XCTAssertThrowsError(try f.ledger.observeArmed(f.physical(control: control, base: base)))
    }

    func testDuplicateInitializeAndRebindPoisonLifetimeEvidenceAndDisposeIsOnce() throws {
        let f = try LossFixture(.writeEnable)
        XCTAssertThrowsError(try f.hub.initialize(command: InitializeCommand(commandId: CommandId(value: 702))))
        XCTAssertThrowsError(try f.prepare())
        try f.hub.dispose()
        XCTAssertEqual(f.wrapper.counts().bind, 1)
        XCTAssertEqual(f.wrapper.counts().initialize, 2)
        XCTAssertEqual(f.wrapper.counts().dispose, 1)
        XCTAssertEqual(f.backend.initializeCalls, 1)
        XCTAssertEqual(f.backend.disposeCalls, 1)
        let second = try LossFixture(.writeEnable)
        defer { try? second.hub.dispose() }
        XCTAssertThrowsError(try BleHub(backend: second.wrapper, downstream: LossSink(second.ledger)))
        XCTAssertThrowsError(try second.exportSetup())
    }

    func testDiscoveredRouteRejectsMissingWrongPropertiesAndZeroOrAliasedIds() throws {
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        for drift in ["missing-service", "missing-ccc", "properties", "zero", "alias"] {
            var database = f.database
            switch drift {
            case "missing-service": database.services = []
            case "missing-ccc": database.services[0].characteristics[1].descriptors = []
            case "properties": database.services[0].characteristics[1].properties.notifiable = false
            case "zero": database.connectionId = ConnectionId(value: 0)
            default: database.services[0].characteristics[1].descriptors[0].id = GattDescriptorId(value: 89)
            }
            XCTAssertThrowsError(try SystemLossRoute(database: database, serviceUuid: LossFixture.service), drift)
        }
        let route = try SystemLossRoute(database: f.database, serviceUuid: LossFixture.service)
        XCTAssertEqual(route.document["read_characteristic_id"] as? String, "89")
        XCTAssertEqual(route.document["updates_characteristic_id"] as? String, "97")
        XCTAssertEqual(route.document["ccc_descriptor_id"] as? String, "107")
    }

    func testDiscoveryAndBaselineAreObservedNotInventedAndTerminalFreezesPrefix() throws {
        for missing in ["discovery", "read", "enabled", "value"] {
            let f = try LossFixture(.disableWrite)
            defer { try? f.hub.dispose() }
            XCTAssertThrowsError(try f.prepare(omit: missing), missing)
        }
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        let setup = try f.prepare()
        let base = try f.ledger.bindControl(f.control(setup))
        f.ledger.stop()
        f.backend.emit(.gattDiscovered(commandId: CommandId(value: 888), database: f.database))
        XCTAssertEqual(try f.ledger.witness(), base)
        XCTAssertThrowsError(try f.ledger.observeArmed(f.physical(control: f.control(setup), base: base)))
        XCTAssertThrowsError(try f.prepare())
    }

    func testRealNativeFailureStopsAdmissionWithoutRewritingPublishedWitness() throws {
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        let control = try f.control(f.prepare())
        let base = try f.ledger.bindControl(control)
        let error = BleEventError(category: .gatt, message: "actual native error", platformDetails: nil)
        f.backend.emit(.backendError(error: error))
        XCTAssertTrue(f.ledger.observationError()?.contains("actual native error") == true)
        XCTAssertThrowsError(try f.ledger.observeArmed(f.physical(control: control, base: base)))
        XCTAssertEqual(try f.ledger.witness(), base)
    }

    func testAtomicFilesFreezeSetupControlAndCumulativePrefixAndStopLateWrites() throws {
        let f = try LossFixture(.writeEnable)
        defer { try? f.hub.dispose() }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let files = try SystemLossWitnessFiles(directory: directory)
        let setup = try f.prepare()
        try files.publishSetup(setup)
        XCTAssertThrowsError(try files.publishSetup(setup))
        let control = try f.control(setup)
        try control.write(to: directory.appendingPathComponent("system-loss-control.json"), options: .atomic)
        try files.freezeControl(control)
        let base = try f.ledger.bindControl(control)
        try files.publishWitness(base)
        var changed = try SystemLossJSON.object(base)
        changed["events"] = []
        XCTAssertThrowsError(try files.publishWitness(SystemLossJSON.canonical(changed)))
        changed = try SystemLossJSON.object(base)
        changed["identity"] = [:]
        XCTAssertThrowsError(try files.publishWitness(SystemLossJSON.canonical(changed)))
        try f.ledger.observeArmed(f.physical(control: control, base: base))
        try f.dispatch()
        let pending = try f.ledger.pending(snapshot: f.lane(), head: f.head, sibling: f.sibling)
        try files.publishWitness(pending)
        XCTAssertEqual(try Data(contentsOf: directory.appendingPathComponent("system-loss-witness.json")), pending)
        let lossStarted = try SystemLossJSON.canonical([
            "schema_version": 1, "state": "loss-started", "session_id": LossFixture.run,
            "control_sha256": try SystemLossJSON.digest(control),
            "witness_sha256": try SystemLossJSON.digest(pending),
            "identity": try SystemLossJSON.object(pending)["identity"] ?? NSNull(),
            "authorization": "host-authorized",
        ] as [String: Any])
        try files.publishLossStarted(lossStarted)
        XCTAssertEqual(try Data(contentsOf: directory.appendingPathComponent("system-loss-loss-started.json")), lossStarted)
        let diagnostic = try SystemLossJSON.canonical([
            "schema_version": 1, "state": "loss-diagnostic", "session_id": LossFixture.run,
            "control_sha256": try SystemLossJSON.digest(control),
            "witness_sha256": try SystemLossJSON.digest(pending),
            "loss": ["last_callback": "none"],
            "resources": ["connections": 1],
        ] as [String: Any])
        try files.publishDiagnostic(diagnostic)
        XCTAssertEqual(try Data(contentsOf: directory.appendingPathComponent("system-loss-diagnostic.json")), diagnostic)
        try Data("changed".utf8).write(to: directory.appendingPathComponent("system-loss-control.json"), options: .atomic)
        XCTAssertThrowsError(try files.verify())
        try files.finish(["state": "stopped", "same_hub": "not-evaluated", "rf": "not-evaluated"])
        XCTAssertThrowsError(try files.publishWitness(base))
        XCTAssertThrowsError(try files.finish(["state": "passed"]))
        XCTAssertEqual(try Data(contentsOf: directory.appendingPathComponent("system-loss-witness.json")), pending)
        XCTAssertThrowsError(try SystemLossWitnessFiles(directory: directory))
    }

    func testCanonicalAsciiHashNullsAndIdBounds() throws {
        let data = try SystemLossJSON.canonical(["z": NSNull(), "a": ["18446744073709551615", true]] as [String: Any])
        XCTAssertEqual(String(decoding: data, as: UTF8.self), "{\"a\":[\"18446744073709551615\",true],\"z\":null}")
        XCTAssertEqual(try SystemLossJSON.digest(Data("{}".utf8)),
                       "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a")
        for invalid in ["0", "01", "-1", "18446744073709551616", "1.0", ""] {
            XCTAssertThrowsError(try SystemLossJSON.id(invalid))
        }
        XCTAssertNoThrow(try SystemLossJSON.id("18446744073709551615"))
        XCTAssertThrowsError(try SystemLossJSON.canonical(["non-ascii": "é"]))
    }

    func testOptInRejectsMixedDuplicateAndIncompleteArgumentsWithoutChangingDefault() throws {
        XCTAssertNil(try SystemLossArguments.parse(["app", "--run-id", LossFixture.run]))
        let args = ["app", "--rf-system-loss-witness", SystemLossCase.writeEnable.rawValue,
                    "--run-id", LossFixture.run, "--rf-system-loss-service-uuid", LossFixture.service,
                    "--rf-system-loss-name-prefix", "DK-B", "--rf-system-loss-write-before", "0000",
                    "--rf-system-loss-write-requested", "1020"]
        XCTAssertNotNil(try SystemLossArguments.parse(args))
        let activeArgs = args.map { $0 == "--rf-system-loss-witness" ? "--rf-system-loss-active" : $0 }
        XCTAssertEqual(try SystemLossArguments.parse(activeArgs)?.active, true)
        XCTAssertEqual(try SystemLossArguments.parse(args)?.active, false)
        for flag in ["--rf-io", "--rf-scan-service-uuid", "--rf-recovery", "--rf-notification", "--rf-connect"] {
            XCTAssertThrowsError(try SystemLossArguments.parse(args + [flag, "x"]))
        }
        XCTAssertThrowsError(try SystemLossArguments.parse(args + ["--run-id", LossFixture.run]))
        XCTAssertThrowsError(try SystemLossArguments.parse(Array(args.dropLast())))
    }

    private func rows(_ data: Data) throws -> [[String: Any]] {
        try XCTUnwrap(SystemLossJSON.object(data)["events"] as? [[String: Any]])
    }
}

private enum LossAdmissionFailure: Error { case busy, notReady }

private final class LossSink: BleEventSink, @unchecked Sendable {
    let ledger: SystemLossWitness
    init(_ ledger: SystemLossWitness) { self.ledger = ledger }
    func onEvent(event: BleEvent) throws { ledger.record(event) }
}

private final class LossFixture {
    static let run = "11111111-2222-4333-8444-555555555555"
    static let service = "fff00000-0000-4000-8000-000000000002"
    let scenario: SystemLossCase
    let ledger = SystemLossWitness()
    let backend = LossBackend()
    let wrapper: SystemLossWitnessBackend
    let hub: BleHub
    let head = CommandId(value: 18446744073709551614)
    let sibling = CommandId(value: 9007199254740993)
    let baselineCommand = CommandId(value: 731)
    let subscription = SubscriptionId(value: 1201)
    let connection = ConnectionId(value: 41)
    let session = GattSessionId(value: 67)
    var database: GattDatabase {
        GattDatabase(connectionId: connection, gattSessionId: session, services: [
            GattService(id: GattServiceId(value: 73), uuid: BluetoothUuid(value: Self.service), primary: true, characteristics: [
                GattCharacteristic(id: GattCharacteristicId(value: 89), uuid: BluetoothUuid(value: SystemLossRoute.readUuid),
                                   properties: CharacteristicProperties(readable: true, writableWithResponse: true, writableWithoutResponse: true, notifiable: false, indicatable: false), descriptors: []),
                GattCharacteristic(id: GattCharacteristicId(value: 97), uuid: BluetoothUuid(value: SystemLossRoute.updatesUuid),
                                   properties: CharacteristicProperties(readable: true, writableWithResponse: false, writableWithoutResponse: false, notifiable: true, indicatable: true),
                                   descriptors: [GattDescriptor(id: GattDescriptorId(value: 107), uuid: BluetoothUuid(value: SystemLossRoute.cccUuid))]),
            ]),
        ])
    }
    init(_ scenario: SystemLossCase) throws {
        self.scenario = scenario
        wrapper = SystemLossWitnessBackend(delegate: backend, witness: ledger)
        hub = try BleHub(backend: wrapper, downstream: LossSink(ledger))
        try hub.initialize(command: InitializeCommand(commandId: CommandId(value: 701)))
    }
    func prepare(omit: String = "") throws -> Data {
        if omit != "discovery" { backend.emit(.gattDiscovered(commandId: CommandId(value: 719), database: database)) }
        if omit != "read" {
            backend.emit(.characteristicRead(commandId: CommandId(value: 727), connectionId: connection,
                                             gattSessionId: session, characteristicId: GattCharacteristicId(value: 89), value: Data([0, 0])))
        }
        if scenario == .disableWrite {
            backend.omit = omit
            try hub.setNotifications(command: notification(baselineCommand, action: .enable(mode: .requireNotification)))
        }
        return try exportSetup()
    }
    func exportSetup() throws -> Data {
        try ledger.prepare(scenario: scenario, sessionId: Self.run, serviceUuid: Self.service,
                                  database: database, initializeCommand: CommandId(value: 701),
                                  readCommand: CommandId(value: 727), before: Data([0, 0]), requested: Data([0x10, 0x20]),
                                  baselineCommand: scenario == .disableWrite ? baselineCommand : nil,
                                  baselineSubscription: scenario == .disableWrite ? subscription : nil)
    }
    func control(_ setup: Data) throws -> Data {
        var object = try SystemLossJSON.object(setup)
        object["token"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        return try SystemLossJSON.canonical(object)
    }
    private var providerTicket: [String: Any] {
        ["wire_hex": String(repeating: "ab", count: 55), "sequence": 1, "handle": 19]
    }
    func physicalHeld(control: Data, pending: Data) throws -> Data {
        let events = try XCTUnwrap(SystemLossJSON.object(pending)["events"] as? [[String: Any]])
        let binding: [String: Any] = [
            "schema_version": 1, "control_sha256": try SystemLossJSON.digest(control),
            "head_command_id": String(head.value), "sibling_command_id": String(sibling.value),
            "subscription_id": String(subscription.value), "witness_sha256": try SystemLossJSON.digest(pending),
        ]
        XCTAssertEqual(events.last?["head_command_id"] as? String, String(head.value))
        return try SystemLossJSON.canonical([
            "schema_version": 1, "scope": "system-loss-provider-only", "session_id": Self.run,
            "control_sha256": SystemLossJSON.digest(control), "state": "held-pending", "radio_authorized": false,
            "recovery": "not-evaluated", "primary_error": NSNull(), "binding": binding,
            "binding_sha256": SystemLossJSON.digest(SystemLossJSON.canonical(binding)),
            "boundaries": [["boundary": "held-pending", "witness": SystemLossJSON.object(pending),
                            "status": ["protocol_version": 2, "ticket": providerTicket, "status_wire": "0200"]]],
        ] as [String: Any])
    }
    func physical(control: Data, base: Data) throws -> Data {
        try SystemLossJSON.canonical([
            "schema_version": 1, "scope": "system-loss-provider-only", "session_id": Self.run,
            "control_sha256": SystemLossJSON.digest(control), "state": "armed", "radio_authorized": false,
            "recovery": "not-evaluated", "primary_error": NSNull(),
            "boundaries": [["boundary": "armed", "witness": SystemLossJSON.object(base),
                                        "status": ["protocol_version": 2, "ticket": providerTicket, "status_wire": "0100"]]],
        ] as [String: Any])
    }
    func notification(_ command: CommandId, action: NotificationAction, drift: String = "") -> SetNotificationsCommand {
        SetNotificationsCommand(commandId: command, connectionId: connection,
                                gattSessionId: GattSessionId(value: drift == "session" ? 999 : session.value),
                                subscriptionId: SubscriptionId(value: drift == "subscription" ? 0 : subscription.value),
                                characteristicId: GattCharacteristicId(value: drift == "characteristic" ? 89 : 97), action: action)
    }
    func pendingOperation(_ command: CommandId, failure: LossAdmissionFailure? = nil) -> SystemLossPendingOperation {
        SystemLossPendingOperation(commandId: command) { timeout in
            if let failure { throw failure }
            try await Task.sleep(nanoseconds: timeout * 1_000_000)
            throw SystemLossFailure.invalid("test operation wait expired")
        }
    }
    func dispatchRole(head isHead: Bool) throws {
        backend.emitNotificationEvents = false
        let command = isHead ? head : sibling
        let write = (scenario == .writeEnable) == isHead
        if write {
            try hub.writeCharacteristic(command: WriteCharacteristicCommand(commandId: command, connectionId: connection,
                gattSessionId: session, characteristicId: GattCharacteristicId(value: 89), value: Data([0x10, 0x20]), writeType: .withResponse))
        } else {
            try hub.setNotifications(command: notification(command, action: scenario == .writeEnable ? .enable(mode: .requireNotification) : .disable))
        }
    }
    func dispatch(drift: String = "") throws {
        backend.emitNotificationEvents = false
        let writeId = scenario == .writeEnable ? head : sibling
        let notificationId = scenario == .writeEnable ? sibling : head
        let write = WriteCharacteristicCommand(commandId: writeId, connectionId: connection, gattSessionId: session,
                                               characteristicId: GattCharacteristicId(value: 89),
                                               value: drift == "payload" ? Data([0xff]) : Data([0x10, 0x20]),
                                               writeType: drift == "write-type" ? .withoutResponse : .withResponse)
        let enabling = (scenario == .writeEnable) != (drift == "case")
        let action: NotificationAction = enabling ? .enable(mode: .requireNotification) : .disable
        let notify = notification(notificationId, action: action, drift: drift)
        if scenario == .writeEnable {
            try hub.writeCharacteristic(command: write)
            try hub.setNotifications(command: notify)
        } else {
            try hub.setNotifications(command: notify)
            try hub.writeCharacteristic(command: write)
        }
    }
    func lane(state: AppleGattIoHeadState = .awaitingNativeCallback, connection: UInt64 = 41,
              head: UInt64 = 18446744073709551614, queued: [UInt64] = [9007199254740993],
              kind: AppleGattIoHeadKind? = .command) -> AppleGattIoSnapshot {
        AppleGattIoSnapshot(connectionId: ConnectionId(value: connection), headKind: kind,
                            headCommandId: CommandId(value: head), headState: state,
                            queuedCommandIds: queued.map { CommandId(value: $0) })
    }
}

private final class LossBackend: BleBackend, @unchecked Sendable {
    var ingress: BackendEventIngressHandle?
    var reject = false
    var omit = ""
    var emitNotificationEvents = true
    var beforeReturn: ((CommandId) -> Void)?
    var dispatched: [CommandId] = []
    var initializeCalls = 0
    var disposeCalls = 0
    func capabilities() throws -> BackendCapabilities {
        BackendCapabilities(platform: .test, contractVersion: blehubContractVersion(), supportsDuplicateScanResults: false,
                            supportsConnectableFlag: false, supportsReadRssi: false, supportsRequiredNotification: true,
                            supportsRequiredIndication: true, reportsActualNotificationMode: true)
    }
    func bindEventIngress(ingress: BackendEventIngressHandle) throws { self.ingress = ingress }
    func emit(_ event: BleEvent) { XCTAssertEqual(ingress?.push(event: event), .delivered) }
    func initialize(command: InitializeCommand) throws {
        initializeCalls += 1
        beforeReturn?(command.commandId)
        if reject { throw SystemLossFailure.invalid("delegate rejected initialize") }
    }
    func writeCharacteristic(command: WriteCharacteristicCommand) throws {
        dispatched.append(command.commandId)
        beforeReturn?(command.commandId)
        if reject {
            emit(.characteristicWritten(commandId: command.commandId, connectionId: command.connectionId,
                                        gattSessionId: command.gattSessionId, characteristicId: command.characteristicId))
            throw SystemLossFailure.invalid("delegate rejected write")
        }
    }
    func setNotifications(command: SetNotificationsCommand) throws {
        dispatched.append(command.commandId)
        if case .enable = command.action, emitNotificationEvents || reject {
            if omit != "enabled" {
                emit(.notificationStateChanged(commandId: command.commandId, connectionId: command.connectionId,
                                               gattSessionId: command.gattSessionId, subscriptionId: command.subscriptionId,
                                               characteristicId: command.characteristicId, state: .enabled(actualMode: .notification)))
            }
            if omit != "value" {
                emit(.notificationReceived(connectionId: command.connectionId, gattSessionId: command.gattSessionId,
                                           subscriptionId: command.subscriptionId, characteristicId: command.characteristicId, value: Data([0xef])))
            }
        }
        beforeReturn?(command.commandId)
        if reject { throw SystemLossFailure.invalid("delegate rejected notifications") }
    }
    func dispose() throws { disposeCalls += 1; ingress = nil }
    private func unexpected() throws { throw SystemLossFailure.invalid("unexpected fake dispatch") }
    func startScan(command: StartScanCommand) throws { try unexpected() }
    func stopScan(command: StopScanCommand) throws { try unexpected() }
    func connect(command: ConnectCommand) throws { try unexpected() }
    func disconnect(command: DisconnectCommand) throws { try unexpected() }
    func discoverGatt(command: DiscoverGattCommand) throws { try unexpected() }
    func readCharacteristic(command: ReadCharacteristicCommand) throws { try unexpected() }
    func readRssi(command: ReadRssiCommand) throws { try unexpected() }
    func cancelOperation(request: CancelOperationRequest) throws { try unexpected() }
}
