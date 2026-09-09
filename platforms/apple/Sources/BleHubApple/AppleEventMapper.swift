import BleHubUniFfi
import CoreBluetooth
import Foundation

internal enum AppleEventMapper {
    static func adapterState(_ state: CBManagerState) -> AdapterState {
        switch state {
        case .unknown, .resetting:
            .unknown
        case .unsupported, .unauthorized:
            .unavailable
        case .poweredOff:
            .poweredOff
        case .poweredOn:
            .poweredOn
        @unknown default:
            .unknown
        }
    }

    static func authorization(_ authorization: CBManagerAuthorization) -> AuthorizationState {
        switch authorization {
        case .notDetermined:
            .notDetermined
        case .restricted:
            .restricted
        case .denied:
            .denied
        case .allowedAlways:
            .authorized
        @unknown default:
            .restricted
        }
    }

    static func adapterSnapshot(
        state: CBManagerState,
        authorization: CBManagerAuthorization
    ) -> AdapterSnapshot {
        let authorization = Self.authorization(authorization)
        return AdapterSnapshot(
            state: adapterState(state),
            scanAuthorization: authorization,
            connectionAuthorization: authorization,
            name: nil
        )
    }

    static func scanAvailabilityError(
        state: CBManagerState,
        authorization: CBManagerAuthorization
    ) -> BleEventError? {
        availabilityError(
            state: state,
            authorization: authorization,
            operation: "scan"
        )
    }

    static func connectionAvailabilityError(
        state: CBManagerState,
        authorization: CBManagerAuthorization
    ) -> BleEventError? {
        availabilityError(
            state: state,
            authorization: authorization,
            operation: "connection"
        )
    }

    static func connectionError(_ error: Error?, context: String) -> BleEventError {
        eventError(category: .connection, error: error, context: context)
    }

    static func gattError(_ error: Error?, context: String) -> BleEventError {
        eventError(category: .gatt, error: error, context: context)
    }

    static func internalError(_ context: String) -> BleEventError {
        eventError(category: .internal, error: nil, context: context)
    }

    private static func eventError(
        category: BleErrorCategory,
        error: Error?,
        context: String
    ) -> BleEventError {
        let platformDetails: PlatformErrorDetails?
        if let error {
            let nsError = error as NSError
            platformDetails = PlatformErrorDetails(
                platform: .apple,
                code: String(nsError.code),
                name: nsError.domain,
                message: nsError.localizedDescription
            )
        } else {
            platformDetails = nil
        }
        return BleEventError(
            category: mappedCategory(error, fallback: category),
            message: context,
            platformDetails: platformDetails
        )
    }

    private static func mappedCategory(
        _ error: Error?,
        fallback: BleErrorCategory
    ) -> BleErrorCategory {
        guard let nsError = error as NSError? else {
            return fallback
        }
        if nsError.domain == CBErrorDomain,
           let code = CBError.Code(rawValue: nsError.code)
        {
            switch code {
            case .operationCancelled:
                return .cancelled
            case .connectionTimeout, .encryptionTimedOut:
                return .timeout
            case .operationNotSupported:
                return .notSupported
            case .invalidParameters, .invalidHandle:
                return .protocol
            default:
                return fallback
            }
        }
        if nsError.domain == CBATTErrorDomain,
           let code = CBATTError.Code(rawValue: nsError.code)
        {
            switch code {
            case .requestNotSupported:
                return .notSupported
            case .insufficientAuthentication, .insufficientAuthorization,
                 .insufficientEncryption, .insufficientEncryptionKeySize:
                return .permission
            case .invalidHandle, .readNotPermitted, .writeNotPermitted,
                 .invalidPdu, .invalidOffset, .attributeNotFound,
                 .attributeNotLong, .invalidAttributeValueLength,
                 .unsupportedGroupType:
                return .protocol
            default:
                return fallback
            }
        }
        return fallback
    }

    private static func availabilityError(
        state: CBManagerState,
        authorization: CBManagerAuthorization,
        operation: String
    ) -> BleEventError? {
        guard authorization == .allowedAlways else {
            return BleEventError(
                category: .permission,
                message: "CoreBluetooth \(operation) authorization is not allowed",
                platformDetails: nil
            )
        }
        guard state == .poweredOn else {
            return BleEventError(
                category: .adapterUnavailable,
                message: "CoreBluetooth adapter is not powered on",
                platformDetails: nil
            )
        }
        return nil
    }
}
