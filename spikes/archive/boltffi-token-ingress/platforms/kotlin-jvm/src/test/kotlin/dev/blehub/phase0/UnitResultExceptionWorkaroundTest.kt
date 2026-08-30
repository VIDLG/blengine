package dev.blehub.phase0

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith

class UnitResultExceptionWorkaroundTest {
    @Test
    fun uncheckedExceptionFromUnitResultIsIncorrectlyReportedAsSuccess() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun unitResult() = error("unchecked unit-result failure")

                override fun successSentinelResult() = true

                override fun recordAckResult() = ExceptionProbeAck(true)
            },
        )

        fixture.callUnitResult()
        fixture.close()
    }

    @Test
    fun checkedSuccessSentinelConvertsSwallowedExceptionToFfiContractFailure() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun unitResult() = Unit

                override fun successSentinelResult(): Boolean =
                    error("unchecked sentinel-result failure")

                override fun recordAckResult() = ExceptionProbeAck(true)
            },
        )

        val error = assertFailsWith<CommandDispatchError.FfiContract> {
            fixture.callCheckedSuccessSentinel()
        }

        assertEquals(
            "foreign callback did not return the required success sentinel",
            error.detail,
        )
        fixture.close()
    }

    @Test
    fun checkedSuccessSentinelAcceptsOnlyExplicitTrue() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun unitResult() = Unit

                override fun successSentinelResult() = true

                override fun recordAckResult() = ExceptionProbeAck(true)
            },
        )

        fixture.callCheckedSuccessSentinel()
        fixture.close()
    }

    @Test
    fun checkedRecordAckConvertsSwallowedExceptionToFfiContractFailure() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun unitResult() = Unit

                override fun successSentinelResult() = true

                override fun recordAckResult(): ExceptionProbeAck =
                    error("unchecked record-ack failure")
            },
        )

        val error = assertFailsWith<CommandDispatchError.FfiContract> {
            fixture.callCheckedRecordAck()
        }

        assertEquals(
            "foreign callback did not return the required success sentinel",
            error.detail,
        )
        fixture.close()
    }

    @Test
    fun checkedRecordAckContainsTenThousandUncheckedExceptions() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun unitResult() = Unit

                override fun successSentinelResult() = true

                override fun recordAckResult(): ExceptionProbeAck =
                    error("unchecked record-ack stress failure")
            },
        )

        repeat(10_000) {
            assertFailsWith<CommandDispatchError.FfiContract> {
                fixture.callCheckedRecordAck()
            }
        }
        fixture.close()
    }

    @Test
    fun checkedRecordAckAcceptsExplicitTrue() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun unitResult() = Unit

                override fun successSentinelResult() = true

                override fun recordAckResult() = ExceptionProbeAck(true)
            },
        )

        fixture.callCheckedRecordAck()
        fixture.close()
    }

    @Test
    fun checkedRecordAckRejectsExplicitFalse() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun unitResult() = Unit

                override fun successSentinelResult() = true

                override fun recordAckResult() = ExceptionProbeAck(false)
            },
        )

        assertFailsWith<CommandDispatchError.FfiContract> {
            fixture.callCheckedRecordAck()
        }
        fixture.close()
    }

    @Test
    fun checkedRecordAckPreservesDeclaredTypedError() {
        val expected = CommandDispatchError.InvalidState(
            CommandKind.START_SCAN,
            "declared probe failure",
        )
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun recordAckResult(): ExceptionProbeAck = throw expected
            },
        )

        assertEquals(
            expected,
            assertFailsWith<CommandDispatchError.InvalidState> {
                fixture.callCheckedRecordAck()
            },
        )
        fixture.close()
    }

    @Test
    fun explicitAckConvertsTenThousandUncheckedExceptionsToFfiContract() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun acknowledgedUnitResult(token: ForeignCallAckToken): Unit =
                    error("unchecked explicit-ACK failure")
            },
        )

        repeat(10_000) {
            assertFailsWith<CommandDispatchError.FfiContract> {
                fixture.callExplicitlyAcknowledgedUnitResult()
            }
        }
        fixture.close()
    }

    @Test
    fun explicitAckRejectsNormalReturnWithoutAcknowledgement() {
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun acknowledgedUnitResult(token: ForeignCallAckToken) = Unit
            },
        )

        assertFailsWith<CommandDispatchError.FfiContract> {
            fixture.callExplicitlyAcknowledgedUnitResult()
        }
        fixture.close()
    }

    @Test
    fun explicitAckAcceptsAcknowledgedReturn() {
        val fixture = ForeignResultExceptionProbeFixture(Probe())

        fixture.callExplicitlyAcknowledgedUnitResult()
        fixture.close()
    }

    @Test
    fun explicitAckPreservesDeclaredTypedErrorBeforeAcknowledgement() {
        val expected = CommandDispatchError.InvalidState(
            CommandKind.START_SCAN,
            "declared explicit-ACK failure",
        )
        val fixture = ForeignResultExceptionProbeFixture(
            object : Probe() {
                override fun acknowledgedUnitResult(token: ForeignCallAckToken): Unit = throw expected
            },
        )

        assertEquals(
            expected,
            assertFailsWith<CommandDispatchError.InvalidState> {
                fixture.callExplicitlyAcknowledgedUnitResult()
            },
        )
        fixture.close()
    }

    private open class Probe : ForeignResultExceptionProbe {
        override fun unitResult() = Unit

        override fun successSentinelResult() = true

        override fun recordAckResult() = ExceptionProbeAck(true)

        override fun acknowledgedUnitResult(token: ForeignCallAckToken) {
            check(acknowledgeForeignCall(token))
        }
    }
}
