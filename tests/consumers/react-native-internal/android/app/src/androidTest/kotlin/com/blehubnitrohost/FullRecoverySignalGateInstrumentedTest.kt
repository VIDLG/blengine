package com.blehubnitrohost

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.Parameterized

/** Deterministic host routing only; these tests never construct a BLE probe. */
class FullRecoverySignalGateInstrumentedTest {
    private val run = "12345678-1234-4234-8234-123456789abc"
    private val tokens = listOf("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
    private val controls = (0..1).map { index -> JSONObject().apply {
        put("session", run); put("flow", index + 8)
        put("mode", if (index == 0) "rf-cancellation" else "rf-watchdog")
        put("token", tokens[index])
    } }
    private val raw get() = JSONObject().put("controls", JSONArray(controls)).toString()
    private val begin get() = "blehub-rn-spike://recover/$run/begin"
    private val urls = controls.flatMap { control ->
        val phases = if (control.getInt("flow") == 8) listOf("armed", "held", "released") else listOf("armed", "held", "released", "recovery")
        phases.map { phase -> "blehub-rf://control/$run/${control.getInt("flow")}/${control.getString("mode")}/${control.getString("token")}/$phase" }
    }
    private fun prepared(): FullRecoverySignalGate = FullRecoverySignalGate(run, raw).also { gate ->
        urls.forEach { assertEquals(FullRecoverySignalGate.Signal.CONTROL, gate.accept(it)) }
    }

    @Test fun allControlsThenBeginThenTenCyclesAreExclusiveAndOrdered() {
        val gate = prepared()
        assertEquals(FullRecoverySignalGate.Signal.BEGIN, gate.accept(begin))
        for (cycle in 0..9) assertEquals(FullRecoverySignalGate.Signal.CYCLE, gate.accept("blehub-rn-spike://recover/$run/$cycle"))
        assertEquals(FullRecoverySignalGate.Signal.REJECTED, gate.accept("blehub-rn-spike://recover/$run/10"))
    }

    @Test fun earlyDuplicateForeignAndOldControlFailClosed() {
        val early = FullRecoverySignalGate(run, raw)
        assertEquals(FullRecoverySignalGate.Signal.REJECTED, early.accept(begin))
        assertEquals(FullRecoverySignalGate.Signal.REJECTED, early.accept(urls[0]))
        for (invalid in listOf(begin + "/", begin + "?x=1", begin + "#x", "blehub-rn-spike://recover/$run/0")) {
            val gate = prepared()
            assertEquals(FullRecoverySignalGate.Signal.REJECTED, gate.accept(invalid))
            assertEquals(FullRecoverySignalGate.Signal.REJECTED, gate.accept(begin))
        }
        for (invalid in listOf(begin, urls.last(), "blehub-rn-spike://recover/$run/1")) {
            val gate = prepared()
            assertEquals(FullRecoverySignalGate.Signal.BEGIN, gate.accept(begin))
            assertEquals(FullRecoverySignalGate.Signal.REJECTED, gate.accept(invalid))
        }
    }

    @Test fun metadataSessionMustMatchAndControlIdentityKeepsLaunchBoot() {
        try {
            FullRecoverySignalGate("00000000-0000-0000-0000-000000000000", raw)
            fail("foreign session accepted")
        } catch (_: IllegalArgumentException) { }
        val gate = FullRecoverySignalGate(run, raw)
        val bootstrap = gate.controlRecord(null, "boot", "bundle", "/app.apk")
        assertEquals(FullRecoverySignalGate.Signal.CONTROL, gate.accept(urls[0]))
        val signal = gate.controlRecord(urls[0], "boot", "bundle", "/app.apk")
        for (key in listOf("session", "pid", "boot_id", "bundle_id", "executable")) assertEquals(bootstrap.get(key), signal.get(key))
        assertEquals("armed", signal.getString("phase"))
    }
}

/** URL admission only; JS readiness and BLE outcomes are deliberately not simulated here. */
class WriteRejectSignalGateInstrumentedTest {
    private val run = "12345678-1234-4234-8234-123456789abc"
    private val token = "11111111-1111-4111-8111-111111111111"
    private val raw get() = JSONObject().apply {
        put("session", run); put("flow", 10); put("mode", "rf-write-reject"); put("token", token)
    }.toString()
    private fun url(phase: String) = "blehub-rf://control/$run/10/rf-write-reject/$token/$phase"

    @Test fun fourPhasesPreserveIdentityAndRejectAnyReplay() {
        val gate = WriteRejectSignalGate(raw)
        val boot = gate.record(null, "boot", "bundle", "/app.apk")
        for (phase in listOf("armed", "held", "rejected", "close-allowed")) {
            assertTrue(gate.accept(url(phase)))
            val signal = gate.record(url(phase), "boot", "bundle", "/app.apk")
            for (key in listOf("session", "flow", "mode", "token", "pid", "boot_id", "bundle_id", "executable"))
                assertEquals(boot.get(key), signal.get(key))
            assertEquals(phase, signal.getString("phase"))
        }
        assertFalse(gate.accept(url("close-allowed")))
        assertFalse(gate.accept(url("armed")))
    }

    @Test fun foreignEarlyDuplicateSuffixAndReloadFailClosed() {
        for (invalid in listOf(url("held"), url("rejected"), url("armed") + "/", url("armed") + "?x=1",
            url("armed") + "#x", url("armed").replace(token, run), url("armed").replace("/10/", "/8/"),
            url("armed").replace("rf-write-reject", "rf-cancellation"), "blehub-rn-spike://reload")) {
            val gate = WriteRejectSignalGate(raw)
            assertFalse(gate.accept(invalid))
            assertFalse(gate.accept(url("armed")))
        }
        val duplicate = WriteRejectSignalGate(raw)
        assertTrue(duplicate.accept(url("armed")))
        assertFalse(duplicate.accept(url("armed")))
        assertFalse(duplicate.accept(url("held")))
        val reload = WriteRejectSignalGate(raw)
        reload.fail()
        assertFalse(reload.accept(url("armed")))
    }

    @Test fun metadataRejectsOldFlowsMalformedUUIDsAndExtraFields() {
        for (raw in listOf(raw.replace("10", "8"), raw.replace("rf-write-reject", "rf-watchdog"),
            raw.replace(token, "BAD"), raw.replace(run, run.uppercase()), JSONObject(raw).put("extra", true).toString())) {
            try { WriteRejectSignalGate(raw); fail("foreign metadata admitted") } catch (_: IllegalArgumentException) { }
        }
    }
}


/** Shared standalone CCC URL admission; neither case is a full-suite index or RF execution. */
@RunWith(Parameterized::class)
class CccEnableRejectSignalGateInstrumentedTest(
    private val mode: String, private val flow: Int, private val phases: List<String>,
) {
    companion object {
        @JvmStatic @Parameterized.Parameters(name = "{0}")
        fun cases(): List<Array<Any>> = listOf(
            arrayOf<Any>("rf-ccc-enable-reject", 11,
                listOf("armed", "held", "rejected", "value-sent", "disable-allowed", "silence-started", "close-allowed")),
            arrayOf<Any>("rf-ccc-disable-reject", 12,
                listOf("baseline-value-sent", "armed", "held", "rejected", "continued-value-sent", "retry-allowed", "silence-started", "close-allowed")),
        )
    }
    private val session = "12345678-1234-4234-8234-123456789abc"
    private val token = "11111111-1111-4111-8111-111111111111"
    private val raw get() = JSONObject().apply {
        put("session", session); put("flow", flow); put("mode", mode); put("token", token)
    }.toString()
    private val prefix get() = "blehub-rf://control/$session/$flow/$mode/$token/"
    private fun gate() = WriteRejectSignalGate(raw, mode)

    @Test fun exactAcksRetainTheAttestedIdentity() {
        val gate = gate()
        for (phase in phases) {
            assertTrue(gate.accept(prefix + phase))
            val record = gate.record(prefix + phase, "boot", "com.blehubnitrohost", "/app/base.apk")
            assertEquals(flow, record.getInt("flow"))
            assertEquals(mode, record.getString("mode"))
            assertEquals(session, record.getString("session"))
            assertEquals(token, record.getString("token"))
            assertEquals(phase, record.getString("phase"))
            assertEquals("boot", record.getString("boot_id"))
        }
        assertFalse(gate.accept(prefix + "close-allowed"))
    }

    @Test fun foreignEarlyDuplicateAndPostRetirementSignalsPoisonTheLedger() {
        for (index in phases.indices) {
            val expected = prefix + phases[index]
            for (invalid in listOf(expected + "/", expected + "?x=1", expected + "#x",
                expected.replace(token, session), expected.replace("/$flow/", "/10/"),
                expected.replace(mode, "rf-write-reject"),
                prefix + phases[(index + 1) % phases.size], "blehub-rn-spike://reload")) {
                val gate = gate()
                phases.take(index).forEach { assertTrue(gate.accept(prefix + it)) }
                assertFalse(gate.accept(invalid))
                assertFalse(gate.accept(expected))
            }
            val duplicate = gate()
            phases.take(index + 1).forEach { assertTrue(duplicate.accept(prefix + it)) }
            assertFalse(duplicate.accept(expected))
        }
        val retired = gate()
        retired.fail()
        assertFalse(retired.accept(prefix + phases.first()))
    }

    @Test fun cccControlCannotBeAdmittedAsWriteRejectOrContinuousTen() {
        for (invalid in listOf(raw.replace(":$flow", ":10"), raw.replace(token, "BAD"),
            JSONObject(raw).put("extra", true).toString(), JSONObject(raw).put("flow", flow.toString()).toString())) {
            try { WriteRejectSignalGate(invalid, mode); fail("invalid CCC control accepted") }
            catch (_: IllegalArgumentException) { }
        }
        try { WriteRejectSignalGate(raw); fail("CCC admitted as write reject") }
        catch (_: IllegalArgumentException) { }
    }
}
