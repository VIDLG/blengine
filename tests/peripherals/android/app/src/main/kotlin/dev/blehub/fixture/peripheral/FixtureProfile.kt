package dev.blehub.fixture.peripheral

import android.content.Context
import java.util.UUID
import org.json.JSONObject

internal data class FixtureProfile(
    val profileId: String,
    val advertising: AdvertisingProfile,
    val services: List<ServiceProfile>,
) {
    companion object {
        fun load(context: Context): FixtureProfile {
            // The Gradle asset root is `tests/conformance/scenarios`, so the archived
            // profile keeps its archive-relative path inside the APK.
            val text = context.assets.open("archive/android/android-peripheral.json")
                .bufferedReader().use { it.readText() }
            val root = JSONObject(text)
            require(root.getInt("schema_version") == 1) { "unsupported fixture schema" }

            val advertisingJson = root.getJSONObject("advertising")
            val advertising = AdvertisingProfile(
                localName = advertisingJson.getString("local_name"),
                serviceUuids = advertisingJson.getJSONArray("service_uuids").mapStrings(UUID::fromString),
            )
            val services = root.getJSONArray("services").mapObjects { serviceJson ->
                ServiceProfile(
                    uuid = UUID.fromString(serviceJson.getString("uuid")),
                    primary = serviceJson.getBoolean("primary"),
                    advertise = serviceJson.optBoolean("advertise", false),
                    characteristics = serviceJson.getJSONArray("characteristics").mapObjects { characteristicJson ->
                        CharacteristicProfile(
                            role = characteristicJson.getString("role"),
                            uuid = UUID.fromString(characteristicJson.getString("uuid")),
                            properties = characteristicJson.getJSONArray("properties")
                                .mapStrings(CharacteristicProperty::fromWire)
                                .toSet(),
                            initialValue = characteristicJson.optStringOrNull("initial_value_hex")
                                ?.decodeHex(),
                        )
                    },
                )
            }
            require(services.isNotEmpty()) { "fixture must define a service" }
            require(advertising.serviceUuids.isNotEmpty()) {
                "fixture must advertise a service UUID"
            }
            require(advertising.serviceUuids.all { advertised ->
                services.any { service -> service.uuid == advertised && service.advertise }
            }) { "every advertised UUID must reference an advertised fixture service" }

            return FixtureProfile(root.getString("profile_id"), advertising, services)
        }
    }
}

internal data class AdvertisingProfile(
    val localName: String,
    val serviceUuids: List<UUID>,
)

internal data class ServiceProfile(
    val uuid: UUID,
    val primary: Boolean,
    val advertise: Boolean,
    val characteristics: List<CharacteristicProfile>,
)

internal data class CharacteristicProfile(
    val role: String,
    val uuid: UUID,
    val properties: Set<CharacteristicProperty>,
    val initialValue: ByteArray?,
)

internal enum class CharacteristicProperty(val wireName: String) {
    READ("read"),
    WRITE("write"),
    WRITE_WITHOUT_RESPONSE("write_without_response"),
    NOTIFY("notify"),
    INDICATE("indicate");

    companion object {
        fun fromWire(value: String): CharacteristicProperty =
            entries.singleOrNull { it.wireName == value }
                ?: error("unsupported characteristic property: $value")
    }
}

private inline fun <T> org.json.JSONArray.mapObjects(transform: (JSONObject) -> T): List<T> =
    List(length()) { index -> transform(getJSONObject(index)) }

private inline fun <T> org.json.JSONArray.mapStrings(transform: (String) -> T): List<T> =
    List(length()) { index -> transform(getString(index)) }

private fun JSONObject.optStringOrNull(name: String): String? =
    if (has(name) && !isNull(name)) getString(name) else null

internal fun String.decodeHex(): ByteArray {
    val normalized = trim().replace(" ", "")
    require(normalized.length % 2 == 0) { "hex value must contain complete bytes" }
    return ByteArray(normalized.length / 2) { index ->
        normalized.substring(index * 2, index * 2 + 2).toInt(16).toByte()
    }
}

internal fun ByteArray.toHex(): String = joinToString("") { byte -> "%02X".format(byte) }
