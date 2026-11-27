import Foundation

struct SystemLossArguments {
    let scenario: SystemLossCase
    let active: Bool
    let runId: String
    let serviceUuid: String
    let characteristicUuid: String?
    let namePrefix: String
    let before: Data
    let requested: Data

    static func parse(_ arguments: [String]) throws -> Self? {
        let witnessFlag = "--rf-system-loss-witness"
        let activeFlag = "--rf-system-loss-active"
        let flags = [witnessFlag, activeFlag].filter { arguments.contains($0) }
        guard flags.count == 1, let flag = flags.first else {
            try systemLossRequire(!arguments.contains { $0.hasPrefix("--rf-system-loss-") }, "orphan or mixed system-loss argument")
            return nil
        }
        let names: Set<String> = [witnessFlag, activeFlag, "--run-id", "--rf-system-loss-service-uuid", "--rf-system-loss-characteristic-uuid", "--rf-system-loss-name-prefix",
                                  "--rf-system-loss-write-before", "--rf-system-loss-write-requested"]
        try systemLossRequire(!arguments.contains { $0.hasPrefix("--rf-") && !names.contains($0) }, "system-loss cannot combine with RF flags")
        func value(_ name: String) throws -> String {
            let positions = arguments.indices.filter { arguments[$0] == name }
            guard positions.count == 1, let index = positions.first, arguments.indices.contains(index + 1),
                  !arguments[index + 1].hasPrefix("--") else {
                throw SystemLossFailure.invalid("one explicit value required for \(name)")
            }
            return arguments[index + 1]
        }
        guard let scenario = SystemLossCase(rawValue: try value(flag)) else {
            throw SystemLossFailure.invalid("unknown system-loss witness case")
        }
        let run = try value("--run-id"), service = try value("--rf-system-loss-service-uuid")
        try SystemLossJSON.uuid(run)
        try SystemLossJSON.uuid(service)
        let characteristic: String?
        if arguments.contains("--rf-system-loss-characteristic-uuid") {
            let value = try value("--rf-system-loss-characteristic-uuid")
            try SystemLossJSON.uuid(value)
            characteristic = value
        } else {
            characteristic = nil
        }
        let prefix = try value("--rf-system-loss-name-prefix")
        try systemLossRequire(!prefix.isEmpty && prefix.utf8.count <= 248, "explicit bounded name prefix required")
        let before = try SystemLossJSON.hex(value("--rf-system-loss-write-before"))
        let requested = try SystemLossJSON.hex(value("--rf-system-loss-write-requested"))
        try systemLossRequire(before != requested && requested.count <= before.count, "write intent bounds")
        return Self(scenario: scenario, active: flag == activeFlag, runId: run, serviceUuid: service, characteristicUuid: characteristic, namePrefix: prefix, before: before, requested: requested)
    }
}
