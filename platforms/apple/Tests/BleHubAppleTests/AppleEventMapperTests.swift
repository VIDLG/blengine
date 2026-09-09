import BleHubUniFfi
import CoreBluetooth
import XCTest
@testable import BleHubApple

final class AppleEventMapperTests: XCTestCase {
    func testMapsEveryCurrentManagerState() {
        XCTAssertEqual(AppleEventMapper.adapterState(.unknown), .unknown)
        XCTAssertEqual(AppleEventMapper.adapterState(.resetting), .unknown)
        XCTAssertEqual(AppleEventMapper.adapterState(.unsupported), .unavailable)
        XCTAssertEqual(AppleEventMapper.adapterState(.unauthorized), .unavailable)
        XCTAssertEqual(AppleEventMapper.adapterState(.poweredOff), .poweredOff)
        XCTAssertEqual(AppleEventMapper.adapterState(.poweredOn), .poweredOn)
    }

    func testMapsEveryCurrentAuthorizationState() {
        XCTAssertEqual(AppleEventMapper.authorization(.notDetermined), .notDetermined)
        XCTAssertEqual(AppleEventMapper.authorization(.restricted), .restricted)
        XCTAssertEqual(AppleEventMapper.authorization(.denied), .denied)
        XCTAssertEqual(AppleEventMapper.authorization(.allowedAlways), .authorized)
    }

    func testSnapshotUsesGlobalAuthorizationForBothOperationClasses() {
        let snapshot = AppleEventMapper.adapterSnapshot(
            state: .poweredOff,
            authorization: .denied
        )

        XCTAssertEqual(snapshot.state, .poweredOff)
        XCTAssertEqual(snapshot.scanAuthorization, .denied)
        XCTAssertEqual(snapshot.connectionAuthorization, .denied)
        XCTAssertNil(snapshot.name)
    }

    func testScanAvailabilityRequiresAuthorizationAndPoweredOnState() {
        XCTAssertNil(
            AppleEventMapper.scanAvailabilityError(
                state: .poweredOn,
                authorization: .allowedAlways
            )
        )
        XCTAssertEqual(
            AppleEventMapper.scanAvailabilityError(
                state: .poweredOff,
                authorization: .allowedAlways
            )?.category,
            .adapterUnavailable
        )
        XCTAssertEqual(
            AppleEventMapper.scanAvailabilityError(
                state: .poweredOn,
                authorization: .denied
            )?.category,
            .permission
        )
    }

    func testMapsCoreBluetoothCancellationTimeoutAndUnsupportedErrors() {
        XCTAssertEqual(
            mappedConnectionCategory(.operationCancelled),
            .cancelled
        )
        XCTAssertEqual(
            mappedConnectionCategory(.connectionTimeout),
            .timeout
        )
        XCTAssertEqual(
            mappedConnectionCategory(.encryptionTimedOut),
            .timeout
        )
        XCTAssertEqual(
            mappedConnectionCategory(.operationNotSupported),
            .notSupported
        )
    }

    func testMapsAttUnsupportedPermissionAndProtocolErrors() {
        XCTAssertEqual(mappedGattCategory(.requestNotSupported), .notSupported)
        XCTAssertEqual(mappedGattCategory(.insufficientAuthorization), .permission)
        XCTAssertEqual(mappedGattCategory(.insufficientEncryption), .permission)
        XCTAssertEqual(mappedGattCategory(.invalidPdu), .protocol)
        XCTAssertEqual(mappedGattCategory(.attributeNotFound), .protocol)
    }

    func testUnknownNativeErrorPreservesFallbackAndPlatformDetails() {
        let nativeError = NSError(
            domain: "dev.blehub.tests.unknown",
            code: 71,
            userInfo: [NSLocalizedDescriptionKey: "native detail"]
        )
        let mapped = AppleEventMapper.gattError(
            nativeError,
            context: "GATT operation failed"
        )

        XCTAssertEqual(mapped.category, .gatt)
        XCTAssertEqual(mapped.message, "GATT operation failed")
        XCTAssertEqual(mapped.platformDetails?.platform, .apple)
        XCTAssertEqual(mapped.platformDetails?.code, "71")
        XCTAssertEqual(mapped.platformDetails?.name, "dev.blehub.tests.unknown")
        XCTAssertEqual(mapped.platformDetails?.message, "native detail")
    }

    private func mappedConnectionCategory(_ code: CBError.Code) -> BleErrorCategory {
        AppleEventMapper.connectionError(
            NSError(domain: CBErrorDomain, code: code.rawValue),
            context: "connection failed"
        ).category
    }

    private func mappedGattCategory(_ code: CBATTError.Code) -> BleErrorCategory {
        AppleEventMapper.gattError(
            NSError(domain: CBATTErrorDomain, code: code.rawValue),
            context: "GATT failed"
        ).category
    }
}
