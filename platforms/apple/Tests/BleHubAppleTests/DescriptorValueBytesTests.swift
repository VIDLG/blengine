import CoreBluetooth
import XCTest
@testable import BleHubApple

/// `CBDescriptor.value` shapes, as CoreBluetooth really delivers them.
final class DescriptorValueBytesTests: XCTestCase {
    /// The case the iOS `descriptor-io` scene hit: a CCCD is an `NSNumber`, and a `Data`-only
    /// cast made every read fail with "completed without a value".
    func testAClientCharacteristicConfigurationArrivesAsANumber() {
        XCTAssertEqual(descriptorValueBytes(NSNumber(value: 0)), [0x00, 0x00])
        XCTAssertEqual(descriptorValueBytes(NSNumber(value: 1)), [0x01, 0x00])
        XCTAssertEqual(descriptorValueBytes(NSNumber(value: 2)), [0x02, 0x00])
    }

    func testBytesPassThroughUnchanged() {
        XCTAssertEqual(descriptorValueBytes(Data([0xde, 0xad])), [0xde, 0xad])
        XCTAssertEqual(descriptorValueBytes(Data()), [])
    }

    func testATextDescriptorIsItsUtf8Bytes() {
        XCTAssertEqual(descriptorValueBytes("hi"), [0x68, 0x69])
    }

    func testAnUnknownOrAbsentValueIsNotGuessed() {
        XCTAssertNil(descriptorValueBytes(nil))
        XCTAssertNil(descriptorValueBytes(Date()))
    }
}
