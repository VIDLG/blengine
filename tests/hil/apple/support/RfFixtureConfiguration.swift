import Foundation

struct RfFixtureConfiguration: Decodable {
    let profileId: String
    let serviceUuid: String
    let hasIndicationOnly: Bool
    let initialValueHex: String
    let notificationValueHex: String
    let writeWithResponseHex: String
    let writeWithoutResponseHex: String

    static let android = RfFixtureConfiguration(
        profileId: "blehub-android-peripheral-v1",
        serviceUuid: "fff00000-0000-4000-8000-000000000000",
        hasIndicationOnly: true,
        initialValueHex: "00",
        notificationValueHex: "deadbeef",
        writeWithResponseHex: "102030",
        writeWithoutResponseHex: "abcd"
    )

    struct InvalidConfiguration: Error, CustomStringConvertible {
        let description: String
    }

    static func fromArguments(_ arguments: [String], serviceUuid: String?) throws -> Self {
        guard let index = arguments.firstIndex(of: "--rf-fixture-config") else { return .android }
        guard arguments.indices.contains(index + 1) else {
            throw InvalidConfiguration(description: "missing --rf-fixture-config JSON")
        }
        let config = try JSONDecoder().decode(Self.self, from: Data(arguments[index + 1].utf8))
        guard let serviceUuid, let expected = UUID(uuidString: serviceUuid),
              UUID(uuidString: config.serviceUuid) == expected,
              config.profileId.range(of: "^[A-Za-z0-9_-]{1,128}$", options: .regularExpression) != nil else {
            throw InvalidConfiguration(description: "fixture profile identity/service UUID mismatch")
        }
        let values = try [config.initialValue, config.writeWithResponse, config.writeWithoutResponse]
        guard Set(values).count == 3 else {
            throw InvalidConfiguration(description: "initial and both write values must differ")
        }
        return config
    }

    private func decodeHex(_ value: String) throws -> Data {
        guard value.range(of: "^(?:[0-9a-fA-F]{2}){1,512}$", options: .regularExpression) != nil else {
            throw InvalidConfiguration(description: "IO values must be 1–512 bytes of unseparated hexadecimal")
        }
        let bytes = Array(value.utf8)
        return Data(stride(from: 0, to: bytes.count, by: 2).map {
            UInt8(String(decoding: bytes[$0 ..< $0 + 2], as: UTF8.self), radix: 16)!
        })
    }

    var initialValue: Data { get throws { try decodeHex(initialValueHex) } }
    var notificationValue: Data { get throws { try decodeHex(notificationValueHex) } }
    var writeWithResponse: Data { get throws { try decodeHex(writeWithResponseHex) } }
    var writeWithoutResponse: Data { get throws { try decodeHex(writeWithoutResponseHex) } }

    var evidence: [String] {
        [
            "apple_rf_profile_id=\(profileId)",
            "apple_rf_service_uuid=\(serviceUuid)",
            "apple_rf_indication_only_required=\(hasIndicationOnly)",
            "apple_rf_initial_value=\(initialValueHex)",
            "apple_rf_notification_value=\(notificationValueHex)",
            "apple_rf_write_with_response_value=\(writeWithResponseHex)",
            "apple_rf_write_without_response_value=\(writeWithoutResponseHex)",
        ]
    }
}
