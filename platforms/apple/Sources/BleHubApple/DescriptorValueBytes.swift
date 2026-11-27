import Foundation

/// The bytes a CoreBluetooth descriptor value actually carries.
///
/// `CBDescriptor.value` is `Any?` and its shape depends on the descriptor:
///
/// - characteristic values arrive as `Data`;
/// - the Client Characteristic Configuration descriptor arrives as an **`NSNumber`** — the
///   one descriptor this project's workbench addresses — so a `Data`-only cast reports "no
///   value" for exactly the case a scene asserts (measured on the iOS `descriptor-io` scene,
///   2026-11-26: `descriptor=failed`, `operation/bleng.operation.gatt`);
/// - a user-description descriptor arrives as a `String`.
///
/// The ATT value is what the peer sent, so an `NSNumber` becomes its 16-bit little-endian
/// encoding — the same bytes Android reports for a CCCD — never a decimal string. A shape
/// this does not know stays `nil`, and the caller reports a failure instead of guessing
/// bytes it cannot justify.
func descriptorValueBytes(_ value: Any?) -> [UInt8]? {
    guard let value else { return nil }
    if let data = value as? Data {
        return [UInt8](data)
    }
    if let number = value as? NSNumber {
        // A CCCD is a 16-bit attribute value; the rest of the ATT payload does not exist.
        let raw = number.uint16Value
        return [UInt8(raw & 0xff), UInt8(raw >> 8)]
    }
    if let text = value as? String {
        return [UInt8](text.utf8)
    }
    return nil
}
