import BleHubUniFfi
import Foundation

/// No-RF lifecycle probe for the application-level owner supervisor. This does
/// not simulate radio loss and does not claim BLE recovery; it only proves that
/// one owner/backend can retire before a fresh owner/backend is assembled.
func verifyAppleFreshOwnerLifecycle(
    runId: String,
    runDirectory: URL,
    status: (@Sendable (String) -> Void)? = nil
) async throws -> String {
    let supervisor = try AppleRustSupervisorOwner(
        queueLabel: "dev.blehub.bleng.ios-fresh-owner",
        allowCentralRecreation: false,
        allowFreshOwner: true
    )
    let hil = AppleRustSupervisorHILAdapter(owner: supervisor)
    do {
        status?("BLeng: Rust supervisor creating owner A")
        let firstStart = try await supervisor.start()
        try hil.configureOldOperationBarrier(oldLink: nil, expectedOldOperationIds: [])
        let oldContext = firstStart.context

        status?("BLeng: Rust supervisor retiring owner A")
        _ = try hil.observeExternal(signal: .radioLost)
        _ = try hil.observeInternalForTest(signal: .radioOn)
        let retirementPlan = try hil.observeInternalForTest(
            signal: .sameOwnerRecoveryFailed(reason: "lifecycle-only fresh-owner probe")
        )
        guard retirementPlan.actions.first == .retireOldOwner else {
            throw SystemLossFailure.invalid("Rust policy did not authorize old-owner retirement")
        }
        _ = try await hil.retireForRecovery()
        let oldRetiredResources = try hil.lastRetiredResourcesForHost()

        let freshContext = ForeignOwnerContext(
            ownerId: UUID().uuidString,
            backendId: UUID().uuidString,
            ownerGeneration: oldContext.ownerGeneration + 1,
            recoveryAttempt: oldContext.recoveryAttempt
        )
        let creationPlan = try await hil.createFreshOwner(context: freshContext)
        guard creationPlan.actions.first == .attemptFreshOwnerRecovery else {
            throw SystemLossFailure.invalid("Rust policy did not create a fresh-owner recovery boundary")
        }
        let secondStart = try hil.activeOwnerStartForHost()
        let newContext = secondStart.context

        status?("BLeng: Rust supervisor retiring owner B")
        _ = try await supervisor.close()
        let newRetiredResources = try hil.lastRetiredResourcesForHost()

        let document: [String: Any] = [
            "schema_version": 1,
            "run_id": runId,
            "diagnostic_scope": "fresh-owner-lifecycle",
            "production_gate_claim": "none",
            "old_session": [
                "generation": oldContext.ownerGeneration,
                "owner_id": oldContext.ownerId,
                "backend_id": oldContext.backendId,
                "adapter_state": String(describing: firstStart.adapterSnapshot.state),
                "scan_authorization": String(describing: firstStart.adapterSnapshot.scanAuthorization),
            ],
            "new_session": [
                "generation": newContext.ownerGeneration,
                "owner_id": newContext.ownerId,
                "backend_id": newContext.backendId,
                "adapter_state": String(describing: secondStart.adapterSnapshot.state),
                "scan_authorization": String(describing: secondStart.adapterSnapshot.scanAuthorization),
            ],
            "old_session_retired_resources": oldRetiredResources,
            "new_session_retired_resources": newRetiredResources,
            "outcome": "fresh-owner-created-and-retired",
        ]
        let data = try JSONSerialization.data(withJSONObject: document, options: [.sortedKeys])
        try data.write(
            to: runDirectory.appendingPathComponent("fresh-owner-lifecycle.json"),
            options: .atomic
        )
        return [
            "apple_bleng_fresh_owner_lifecycle=attempted",
            "fresh_owner_lifecycle_outcome=fresh-owner-created-and-retired",
            "old_owner_id=\(oldContext.ownerId)",
            "new_owner_id=\(newContext.ownerId)",
            "old_backend_id=\(oldContext.backendId)",
            "new_backend_id=\(newContext.backendId)",
            "production_gate_claim=none",
            "fresh_owner_lifecycle_diagnostic=fresh-owner-lifecycle.json",
        ].joined(separator: "\n") + "\n"
    } catch {
        try? await supervisor.close()
        throw error
    }
}
