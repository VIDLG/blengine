package com.blehubblenghost

import androidx.test.platform.app.InstrumentationRegistry
import com.facebook.react.bridge.BridgeReactContext
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.soloader.nativeloader.NativeLoader
import com.facebook.soloader.nativeloader.SystemDelegate
import com.margelo.nitro.JNIOnLoad
import com.margelo.nitro.NitroModules
import com.margelo.nitro.blehubreactnativebleng.BleHubReactNativeBlengPackage
import com.margelo.nitro.core.HybridObject
import com.margelo.nitro.blehubreactnativebleng.BlengAdapterSnapshot
import com.margelo.nitro.blehubreactnativebleng.BlengAdapterState
import com.margelo.nitro.blehubreactnativebleng.BlengAuthorizationState
import com.margelo.nitro.blehubreactnativebleng.BlengCloseResult
import com.margelo.nitro.blehubreactnativebleng.HybridBlengSessionOwner
import com.margelo.nitro.blehubreactnativebleng.toBlengAdapterSnapshot
import com.margelo.nitro.core.Promise
import com.blehub.reactnative.bleng.AndroidRustSupervisorOwner
import com.margelo.nitro.blehubreactnativebleng.BlengScanAdvertisement
import com.margelo.nitro.blehubreactnativebleng.BlengScanFailureDomain
import com.margelo.nitro.blehubreactnativebleng.BlengScanFailureInfo
import com.margelo.nitro.blehubreactnativebleng.BlengScanEndReason
import com.margelo.nitro.blehubreactnativebleng.BlengScanNext
import com.margelo.nitro.blehubreactnativebleng.BlengScanOptions

import com.margelo.nitro.blehubreactnativebleng.BlengScanPlatformDetails
import com.margelo.nitro.blehubreactnativebleng.BlengScanStopSubmission
import com.margelo.nitro.blehubreactnativebleng.BlengScanSubmission
import com.margelo.nitro.blehubreactnativebleng.HybridBlengPeripheralSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengScan
import com.margelo.nitro.blehubreactnativebleng.HybridBlengScanSpec
import com.margelo.nitro.blehubreactnativebleng.HybridBlengScanStop
import dev.blehub.AdvertisementObservation
import dev.blehub.BackendCapabilities
import dev.blehub.BackendEventIngressHandle
import dev.blehub.BleBackend
import dev.blehub.BleErrorCategory
import dev.blehub.BleEvent
import dev.blehub.BleEventError
import dev.blehub.BluetoothUuid
import dev.blehub.EventIngressStatus
import dev.blehub.InitializeCommand
import dev.blehub.ManufacturerData
import dev.blehub.PeripheralId
import dev.blehub.Platform
import dev.blehub.PlatformErrorDetails
import dev.blehub.ScanStopReason
import dev.blehub.ScanStopResult
import dev.blehub.ServiceData
import dev.blehub.StartScanCommand
import dev.blehub.StopScanCommand
import dev.blehub.blehubContractVersion
import dev.bleng.Bleng
import dev.bleng_supervisor.ForeignOwnerClose
import dev.bleng_supervisor.ForeignOwnerContext
import dev.bleng_supervisor.ForeignOwnerStart
import dev.bleng_supervisor.ForeignPlatformActionResult
import dev.bleng_supervisor.ForeignPlatformOwnerSession
import dev.bleng_supervisor.ForeignPlatformOwnerSessionFactory
import dev.bleng_supervisor.ForeignPlatformRecoveryAction
import dev.bleng_supervisor.ForeignResourceCount
import dev.bleng_supervisor.ForeignScanAdvertisement
import dev.bleng_supervisor.ForeignScanBeginResult
import dev.bleng_supervisor.ForeignScanCommandResult
import dev.bleng_supervisor.ForeignScanEnd
import dev.bleng_supervisor.ForeignScanEndReason
import dev.bleng_supervisor.ForeignScanFailure
import dev.bleng_supervisor.ForeignScanFailureDomain
import dev.bleng_supervisor.ForeignScanObservation
import dev.bleng_supervisor.ForeignScanOptions
import dev.bleng_supervisor.ForeignScanStopResult
import dev.bleng_supervisor.ScanEventProjection
import dev.bleng_supervisor.SupervisedPeripheral
import dev.bleng_supervisor.SupervisedScan
import dev.bleng_supervisor.SupervisedScanStop
import dev.bleng_supervisor.Supervisor
import dev.bleng_supervisor.NoHandle as SupervisorNoHandle
import java.lang.reflect.Proxy
import java.util.ArrayDeque
import kotlin.coroutines.CoroutineContext
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.cancel
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import dev.blehub.AdapterSnapshot
import dev.blehub.AdapterState
import dev.blehub.AuthorizationState
import dev.bleng_supervisor.SupervisorFfiException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** Formal lifecycle compatibility and fake-backend scan contracts; no scan test uses real BLE/RF. */
class HybridBlengSessionOwnerInstrumentedTest {
    private lateinit var owner: HybridBlengSessionOwner
    private var previousReactContext: ReactApplicationContext? = null
    private var reactContextInstalled = false

    @Before
    fun initializeNitroRuntime() {
        NativeLoader.initIfUninitialized(SystemDelegate())
        JNIOnLoad.initializeNativeNitro()
        previousReactContext = NitroModules.applicationContext
        NitroModules.applicationContext = testReactContext()
        reactContextInstalled = true
        BleHubReactNativeBlengPackage()
        owner = HybridBlengSessionOwner()
    }

    @After
    fun closeOwnerAndRestoreReactContext() {
        try {
            if (this::owner.isInitialized) {
                assertTrue(await(owner.close()).ownerClosed)
            }
        } finally {
            if (reactContextInstalled) {
                NitroModules.applicationContext = previousReactContext
            }
        }
    }

    @Test
    fun formalPackageInitializesGeneratedJniBeforeHybridCreation() {
        // Kotlin-only construction is lazy and previously hid missing JNI registration.
        // This is the same SDK entry the generated registry constructor uses.
        val getCxxPart = HybridObject::class.java.getDeclaredMethod("getCxxPart").apply {
            isAccessible = true
        }
        try {
            val cxxPart = getCxxPart.invoke(owner)
            assertNotNull(cxxPart)
            BleHubReactNativeBlengPackage()
            assertTrue(cxxPart === getCxxPart.invoke(owner))
        } finally {
            owner.dispose()
        }
    }

    @Test
    fun closeIsIdempotentAndOwnerCanRestart() {
        val empty: BlengCloseResult = await(owner.close())
        assertTrue(empty.ownerClosed)
        assertNull(empty.failure)
        assertNotNull(await<BlengAdapterSnapshot>(owner.start()))
        val closed = await(owner.close())
        assertTrue(closed.ownerClosed)
        assertNull(closed.failure)
        assertEquals(closed, await(owner.close()))
        assertNotNull(await<BlengAdapterSnapshot>(owner.start()))
    }

    @Test
    fun duplicateStartRejectsWithNativeErrorAndDoesNotPreventRestart() {
        assertNotNull(await<BlengAdapterSnapshot>(owner.start()))

        val failure = awaitSettlement(owner.start()).exceptionOrNull()
        assertTrue(
            "Duplicate start must reject with the native supervisor error, got $failure",
            failure is SupervisorFfiException.Platform,
        )
        assertEquals(
            "supervisor already has an active session",
            (failure as SupervisorFfiException.Platform).detail,
        )

        assertTrue(await(owner.close()).ownerClosed)
        assertNotNull(await<BlengAdapterSnapshot>(owner.start()))
    }

    @Test
    fun adapterSnapshotProjectionPreservesEveryStateAuthorizationAndName() {
        val states = listOf(
            AdapterState.UNKNOWN to BlengAdapterState.UNKNOWN,
            AdapterState.UNAVAILABLE to BlengAdapterState.UNAVAILABLE,
            AdapterState.POWERED_OFF to BlengAdapterState.POWEREDOFF,
            AdapterState.POWERED_ON to BlengAdapterState.POWEREDON,
        )
        val authorizations = listOf(
            AuthorizationState.NOT_REQUIRED to BlengAuthorizationState.NOTREQUIRED,
            AuthorizationState.NOT_DETERMINED to BlengAuthorizationState.NOTDETERMINED,
            AuthorizationState.DENIED to BlengAuthorizationState.DENIED,
            AuthorizationState.RESTRICTED to BlengAuthorizationState.RESTRICTED,
            AuthorizationState.AUTHORIZED to BlengAuthorizationState.AUTHORIZED,
        )
        assertEquals(AdapterState.values().toSet(), states.map { it.first }.toSet())
        assertEquals(AuthorizationState.values().toSet(), authorizations.map { it.first }.toSet())
        assertEquals(BlengAdapterState.values().toSet(), states.map { it.second }.toSet())
        assertEquals(BlengAuthorizationState.values().toSet(), authorizations.map { it.second }.toSet())

        for ((state, expectedState) in states) {
            for ((scanAuthorization, expectedScanAuthorization) in authorizations) {
                for ((connectionAuthorization, expectedConnectionAuthorization) in authorizations) {
                    for (name in listOf(null, "", "蓝牙 📡 café")) {
                        val snapshot = AdapterSnapshot(
                            state = state,
                            scanAuthorization = scanAuthorization,
                            connectionAuthorization = connectionAuthorization,
                            name = name,
                        )
                        assertEquals(
                            "Projection of $snapshot",
                            BlengAdapterSnapshot(
                                state = expectedState,
                                scanAuthorization = expectedScanAuthorization,
                                connectionAuthorization = expectedConnectionAuthorization,
                                name = name,
                            ),
                            snapshot.toBlengAdapterSnapshot(),
                        )
                    }
                }
            }
        }
    }

    @Test
    fun scanWithoutStartupNeverCreatesAnOwnerOrSubmitsBle() {
        repeat(2) {
            val failure = requireNotNull(owner.beginScan(scanOptions()).asSecondOrNull()).failure
            assertEquals(BlengScanFailureDomain.LIFECYCLE, failure.domain)
            assertEquals("submissionUnavailable", failure.code)
            assertEquals("No started session is available for scan submission.", failure.message)
            assertNull(failure.platform)
            assertNull(rootOwnerField().get(owner))
        }
        await(owner.close())
        assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
    }

    @Test
    fun pendingStartupAndUnexpectedCloseThrowKeepSubmissionGateClosed() = withScanFixture(autoInitialize = false) { fixture ->
        val starting = owner.start()
        runBlocking { withTimeout(5_000) { fixture.initializing.await() } }
        assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
        assertEquals(0, fixture.scanCount.get())
        fixture.completeInitialization()
        await(starting)
        val nativeOwner = rootOwnerField().get(owner) as AndroidRustSupervisorOwner
        val supervisorField = AndroidRustSupervisorOwner::class.java.getDeclaredField("supervisor").apply {
            isAccessible = true
        }
        val original = supervisorField.get(nativeOwner) as Supervisor
        supervisorField.set(nativeOwner, object : Supervisor(SupervisorNoHandle) {
            private var failOnce = true
            override fun beginScanView(context: ForeignOwnerContext, options: ForeignScanOptions): ForeignScanBeginResult =
                throw IllegalStateException("secret-native-context")
            override suspend fun shutdownView(): ForeignOwnerClose {
                if (failOnce) {
                    failOnce = false
                    throw IllegalStateException("synthetic close dispatch failure")
                }
                return original.shutdownView()
            }
        })
        assertBridgeFailure(requireNotNull(owner.beginScan(scanOptions()).asSecondOrNull()).failure)
        val closing = owner.close()
        assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
        assertNotNull(awaitSettlement(closing).exceptionOrNull())
        assertNotNull(rootOwnerField().get(owner))
        assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
        await(owner.close())
    }

    @Test
    fun realInitializationFailureClosesWithFailureAndExplicitStartCreatesFreshOwner() {
        val fresh = ScanFixture(true)
        withScanFixture(rejectFirstInitialization = true, replacementFixture = fresh) { failed ->
            assertNotNull(awaitSettlement(owner.start()).exceptionOrNull())
            val oldOwner = rootOwnerField().get(owner) as AndroidRustSupervisorOwner
            val closed = await(owner.close())
            assertTrue(closed.ownerClosed)
            assertEquals("no_active_session", closed.failure?.code)
            assertEquals("supervisor has no active session", closed.failure?.message)
            assertNull(rootOwnerField().get(owner))
            assertEquals(closed, await(owner.close()))
            val native = runBlocking { oldOwner.closeResult() }
            assertEquals(native.ownerClosed, closed.ownerClosed)
            assertEquals(native.failure?.code, closed.failure?.code)
            assertEquals(native.failure?.message, closed.failure?.message)
            assertEquals(1, failed.disposeCount.get())
            assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)

            await(owner.start())
            assertTrue(rootOwnerField().get(owner) !== oldOwner)
            val scan = acceptedScan()
            assertNull(await(scan.waitUntilStarted(5_000.0)).failure)
            assertEquals(1, fresh.scanCount.get())
            val restartedClose = await(owner.close())
            assertTrue(restartedClose.ownerClosed)
            assertNull(restartedClose.failure)
            assertEquals(1, fresh.disposeCount.get())
        }
    }

    @Test
    fun realCleanupFailureRemainsTerminalWithoutRepeatedDisposeAndExplicitStartClearsReceipt() {
        val fresh = ScanFixture(true)
        withScanFixture(reportRetirementLeak = true, replacementFixture = fresh) { failed ->
            await(owner.start())
            val oldOwner = rootOwnerField().get(owner) as AndroidRustSupervisorOwner
            val closed = await(owner.close())
            assertTrue(closed.ownerClosed)
            assertEquals("native_resources_not_zero", closed.failure?.code)
            assertEquals("native resources remained after retirement", closed.failure?.message)
            assertNull(rootOwnerField().get(owner))
            repeat(2) {
                assertEquals(closed, await(owner.close()))
                assertEquals(1, failed.disposeCount.get())
            }
            val native = runBlocking { oldOwner.closeResult() }
            assertEquals(native.ownerClosed, closed.ownerClosed)
            assertEquals(native.failure?.code, closed.failure?.code)
            assertEquals(native.failure?.message, closed.failure?.message)
            assertEquals(1, failed.disposeCount.get())

            await(owner.start())
            assertTrue(rootOwnerField().get(owner) !== oldOwner)
            val restartedClose = await(owner.close())
            assertTrue(restartedClose.ownerClosed)
            assertNull(restartedClose.failure)
            assertEquals(1, fresh.disposeCount.get())
        }
    }

    @Test
    fun realBusyCloseRetainsOwnerAndLaterCloseCanFinish() = withScanFixture(autoInitialize = false) { fixture ->
        val nativeOwner = rootOwnerField().get(owner) as AndroidRustSupervisorOwner
        // The root mutex serializes its own calls; suspend a real Rust start through the native seam.
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        val starting = scope.async { nativeOwner.start() }
        try {
            runBlocking { withTimeout(5_000) { fixture.initializing.await() } }
            repeat(2) {
                val busy = await(owner.close())
                assertFalse(busy.ownerClosed)
                assertEquals("supervisor_busy", busy.failure?.code)
                assertEquals("supervisor operation is already in progress", busy.failure?.message)
                assertTrue(rootOwnerField().get(owner) === nativeOwner)
                assertEquals(0, fixture.disposeCount.get())
                assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
            }
        } finally {
            fixture.completeInitialization()
            runBlocking { withTimeout(5_000) { starting.await() } }
            scope.cancel()
        }
        val closed = await(owner.close())
        assertTrue(closed.ownerClosed)
        assertNull(closed.failure)
        assertNull(rootOwnerField().get(owner))
        assertEquals(closed, await(owner.close()))
        assertEquals(1, fixture.disposeCount.get())
    }

    @Test
    fun closeExecutedBeforeQueuedOldStartRejectsWithoutConstructingOwner() {
        val dispatcher = QueuedLifecycleDispatcher()
        val testScope = CoroutineScope(SupervisorJob() + dispatcher)
        val scopeField = HybridBlengSessionOwner::class.java.getDeclaredField("scope").apply { isAccessible = true }
        val factoryField = HybridBlengSessionOwner::class.java.getDeclaredField("ownerFactory").apply { isAccessible = true }
        val previousScope = scopeField.get(owner)
        val previousFactory = factoryField.get(owner)
        val constructions = AtomicInteger()
        val forbiddenFactory: () -> AndroidRustSupervisorOwner = {
            constructions.incrementAndGet()
            error("A revoked start must not construct an owner")
        }
        scopeField.set(owner, testScope)
        factoryField.set(owner, forbiddenFactory)
        try {
            val starting = owner.start()
            val closing = owner.close()
            dispatcher.runLast()
            assertTrue(await(closing).ownerClosed)
            assertNull(rootOwnerField().get(owner))
            dispatcher.runFirst()
            val failure = awaitSettlement(starting).exceptionOrNull()
            assertTrue(failure is IllegalStateException)
            assertEquals("Session start was superseded by close.", failure?.message)
            assertEquals(0, constructions.get())
            assertNull(rootOwnerField().get(owner))
            assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
        } finally {
            testScope.cancel()
            scopeField.set(owner, previousScope)
            factoryField.set(owner, previousFactory)
        }
    }

    @Test
    fun duplicateStartRejectionPreservesTheActualReceiptAndScanSubmission() = withScanFixture { fixture ->
        await(owner.start())
        val entered = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val nativeOwner = rootOwnerField().get(owner) as AndroidRustSupervisorOwner
        val field = AndroidRustSupervisorOwner::class.java.getDeclaredField("supervisor").apply { isAccessible = true }
        val original = field.get(nativeOwner) as Supervisor
        field.set(nativeOwner, object : Supervisor(SupervisorNoHandle) {
            override suspend fun start(): ForeignOwnerStart {
                entered.complete(Unit)
                release.await()
                return original.start()
            }
            override fun beginScanView(context: ForeignOwnerContext, options: ForeignScanOptions) =
                original.beginScanView(context, options)
            override suspend fun shutdownView() = original.shutdownView()
        })
        val duplicate = owner.start()
        try {
            runBlocking { withTimeout(5_000) { entered.await() } }
            assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
            assertEquals(0, fixture.scanCount.get())
        } finally {
            release.complete(Unit)
        }
        val failure = awaitSettlement(duplicate).exceptionOrNull()
        assertTrue(failure is SupervisorFfiException.Platform)
        assertEquals("supervisor already has an active session", (failure as SupervisorFfiException.Platform).detail)
        val scan = acceptedScan()
        assertNull(await(scan.waitUntilStarted(5_000.0)).failure)
        assertEquals(1, fixture.scanCount.get())
        fixture.advertise()
        assertTrue(await(scan.nextEvent(5_000.0)).isFirst)
    }

    @Test
    fun duplicateStartFailureCannotRestoreReceiptAcrossUnexpectedCloseThrow() = withScanFixture { fixture ->
        await(owner.start())
        val entered = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val nativeOwner = rootOwnerField().get(owner) as AndroidRustSupervisorOwner
        val field = AndroidRustSupervisorOwner::class.java.getDeclaredField("supervisor").apply { isAccessible = true }
        val original = field.get(nativeOwner) as Supervisor
        field.set(nativeOwner, object : Supervisor(SupervisorNoHandle) {
            private var failCloseOnce = true
            override suspend fun start(): ForeignOwnerStart {
                entered.complete(Unit)
                release.await()
                return original.start()
            }
            override suspend fun shutdownView(): ForeignOwnerClose {
                if (failCloseOnce) {
                    failCloseOnce = false
                    throw IllegalStateException("synthetic close dispatch failure")
                }
                return original.shutdownView()
            }
        })
        val duplicate = owner.start()
        try {
            runBlocking { withTimeout(5_000) { entered.await() } }
            val closing = owner.close()
            release.complete(Unit)
            assertTrue(awaitSettlement(duplicate).exceptionOrNull() is SupervisorFfiException.Platform)
            assertNotNull(awaitSettlement(closing).exceptionOrNull())
            assertNotNull(rootOwnerField().get(owner))
            assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
            assertEquals(0, fixture.scanCount.get())
            await(owner.close())
        } finally {
            release.complete(Unit)
        }
    }

    @Test
    fun queuedCloseCannotBeReopenedByCompletingStartup() = withScanFixture(autoInitialize = false) { fixture ->
        val starting = owner.start()
        runBlocking { withTimeout(5_000) { fixture.initializing.await() } }
        val nativeOwner = rootOwnerField().get(owner) as AndroidRustSupervisorOwner
        val supervisorField = AndroidRustSupervisorOwner::class.java.getDeclaredField("supervisor").apply {
            isAccessible = true
        }
        val original = supervisorField.get(nativeOwner) as Supervisor
        val enteredClose = CompletableDeferred<Unit>()
        val releaseClose = CompletableDeferred<Unit>()
        supervisorField.set(nativeOwner, object : Supervisor(SupervisorNoHandle) {
            override suspend fun shutdownView(): ForeignOwnerClose {
                enteredClose.complete(Unit)
                releaseClose.await()
                return original.shutdownView()
            }
        })
        val closing = owner.close()
        try {
            fixture.completeInitialization()
            await(starting)
            runBlocking { withTimeout(5_000) { enteredClose.await() } }
            assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
            assertEquals(0, fixture.scanCount.get())
        } finally {
            releaseClose.complete(Unit)
            await(closing)
        }
        assertNull(rootOwnerField().get(owner))
        assertEquals("submissionUnavailable", owner.beginScan(scanOptions()).asSecondOrNull()?.failure?.code)
    }

    @Test
    fun advertisementCopiesOwnedBytesAndPreservesAbsentValuesAndOpaqueCapability() = withScanFixture { fixture ->
        await(owner.start())
        val scan = acceptedScan()
        assertNull(await(scan.waitUntilStarted(5_000.0)).failure)
        fixture.advertise()
        val value = requireNotNull(await(scan.nextEvent(5_000.0)).asFirstOrNull()).advertisement
        assertNull(value.localName)
        assertNull(value.rssi)
        assertNull(value.connectable)
        assertArrayEquals(arrayOf(SERVICE_UUID), value.advertisedServices)
        assertEquals(65535.0, value.manufacturerData.single().companyId, 0.0)
        val payload = value.manufacturerData.single().payload
        assertTrue(payload.isOwner)
        assertArrayEquals(byteArrayOf(0, 127, -128, -1), payload.toByteArray())
        assertTrue(value.serviceData.single().payload.isOwner)
        assertEquals(0, value.serviceData.single().payload.size)
        assertEquals(SERVICE_UUID, value.serviceData.single().serviceUuid)
        fixture.manufacturerBytes.fill(42)
        assertArrayEquals(byteArrayOf(0, 127, -128, -1), payload.toByteArray())
        assertEquals("[HybridObject BlengPeripheral]", value.peripheral.toString())
        assertTrue(HybridBlengPeripheralSpec::class.java.declaredMethods.none {
            it.name.contains("context", true) || it.name.contains("id", true)
        })
        val opaque = value.peripheral.javaClass.declaredFields.single { !it.isSynthetic }
        assertEquals(SupervisedPeripheral::class.java, opaque.type)
        assertTrue(java.lang.reflect.Modifier.isPrivate(opaque.modifiers))
        fixture.advertise(localName = "蓝牙 📡 café", rssi = (-127).toShort(), connectable = false)
        val present = requireNotNull(await(scan.nextEvent(5_000.0)).asFirstOrNull()).advertisement
        assertEquals("蓝牙 📡 café", present.localName)
        assertEquals(-127.0, requireNotNull(present.rssi), 0.0)
        assertEquals(false, present.connectable)

        // UniFFI already copies the fixture bytes; probe the mapper's own copy boundary separately.
        val sourceBytes = byteArrayOf(0, -128, -1)
        val dto = ForeignScanObservation(ForeignScanAdvertisement(
            SupervisedPeripheral(SupervisorNoHandle), null, emptyList(),
            listOf(ManufacturerData(1u.toUShort(), sourceBytes)),
            listOf(ServiceData(BluetoothUuid(SERVICE_UUID), sourceBytes)), null, null,
        ), null, null)
        val copied = requireNotNull((mapScanDto("observation", dto) as BlengScanNext).asFirstOrNull()).advertisement
        sourceBytes.fill(7)
        assertArrayEquals(byteArrayOf(0, -128, -1), copied.manufacturerData.single().payload.toByteArray())
        assertArrayEquals(byteArrayOf(0, -128, -1), copied.serviceData.single().payload.toByteArray())
        copied.manufacturerData.single().payload.getBuffer(false).put(0, 42.toByte())
        assertArrayEquals(byteArrayOf(7, 7, 7), sourceBytes)
        assertArrayEquals(byteArrayOf(0, -128, -1), copied.serviceData.single().payload.toByteArray())
    }

    @Test
    fun rustValidatesCompanyAndEveryTimeoutWithoutKotlinTruncation() = withScanFixture { fixture ->
        await(owner.start())
        for (company in listOf(Double.NaN, Double.POSITIVE_INFINITY, -1.0, 0.5, 65536.0)) {
            val failure = requireNotNull(owner.beginScan(scanOptions(company)).asSecondOrNull()).failure
            assertEquals(BlengScanFailureDomain.INPUT, failure.domain)
            assertEquals("invalid_company_id", failure.code)
        }
        assertEquals(0, fixture.scanCount.get())
        val scan = acceptedScan()
        for (timeout in listOf(Double.NaN, Double.POSITIVE_INFINITY, -1.0, 0.5, 0.0, 86_400_001.0, 18446744073709551616.0)) {
            assertEquals(BlengScanFailureDomain.INPUT, await(scan.waitUntilStarted(timeout)).failure?.domain)
            assertEquals(BlengScanFailureDomain.INPUT, await(scan.nextEvent(timeout)).asThirdOrNull()?.failure?.domain)
        }
        assertNull(await(scan.waitUntilStarted(5_000.0)).failure)
        val stop = requireNotNull(scan.beginStop().asFirstOrNull()).operation
        assertEquals(BlengScanFailureDomain.INPUT, await(stop.wait(Double.NaN)).failure?.domain)
        assertNull(await(stop.wait(5_000.0)).failure)
        assertEquals(1, fixture.scanCount.get())
        assertEquals(1, fixture.stopCount.get())
    }

    @Test
    fun typedRustFailuresPreservePlatformDetailsWithoutExposingIds() = withScanFixture { fixture ->
        await(owner.start())
        val scan = acceptedScan()
        assertNull(await(scan.waitUntilStarted(5_000.0)).failure)
        fixture.endWithFailure()
        val ended = requireNotNull(await(scan.nextEvent(5_000.0)).asSecondOrNull()).ended
        assertEquals(BlengScanEndReason.FAILED, ended.reason)
        val failure = requireNotNull(ended.failure)
        assertEquals(BlengScanFailureDomain.OPERATION, failure.domain)
        assertEquals("ble.permission", failure.code)
        assertEquals("permission denied by synthetic backend", failure.message)
        assertEquals(BlengScanPlatformDetails("android", "13", "Permission", "denied"), failure.platform)
        assertFalse(failure.toString().contains("ownerId"))
        assertTrue(BlengScanAdvertisement::class.java.declaredFields.none {
            it.name in listOf("peripheralId", "scanId", "commandId", "context", "ownerId", "backendId")
        })
    }

    @Test
    fun singlePullRejectionAndTimeoutDoNotDetachOrStopDelivery() = withScanFixture { fixture ->
        await(owner.start())
        val scan = acceptedScan()
        assertNull(await(scan.waitUntilStarted(5_000.0)).failure)
        val timedOut = requireNotNull(await(scan.nextEvent(1.0)).asThirdOrNull()).failure
        assertEquals(BlengScanFailureDomain.OBSERVATION, timedOut.domain)
        // Both calls enter Rust; whichever owns the slot remains pending until the advertisement.
        val rejected = CountDownLatch(1)
        val first = scan.nextEvent(5_000.0)
        val second = scan.nextEvent(5_000.0)
        for (promise in listOf(first, second)) {
            promise.then { if (it.asThirdOrNull()?.failure?.code == "delivery.pull_busy") rejected.countDown() }
        }
        assertTrue("Rust did not reject the overlapping pull", rejected.await(3, TimeUnit.SECONDS))
        fixture.advertise()
        val results = listOf(await(first), await(second))
        assertEquals(1, results.count { it.isFirst })
        assertEquals(listOf("delivery.pull_busy"), results.mapNotNull { it.asThirdOrNull()?.failure?.code })
        assertEquals(0, fixture.stopCount.get())
        fixture.advertise()
        assertTrue(await(scan.nextEvent(5_000.0)).isFirst)
    }

    @Test
    fun actualStopAndOwnerRetirementAreDistinctAndDisposeOnlyDetaches() = withScanFixture { fixture ->
        await(owner.start())
        val scan = acceptedScan()
        assertNull(await(scan.waitUntilStarted(5_000.0)).failure)
        val stop = requireNotNull(scan.beginStop().asFirstOrNull()).operation
        assertNull(await(stop.wait(5_000.0)).failure)
        assertEquals(BlengScanEndReason.REQUESTED, await(scan.nextEvent(5_000.0)).asSecondOrNull()?.ended?.reason)
        val detached = acceptedScan()
        assertNull(await(detached.waitUntilStarted(5_000.0)).failure)
        detached.dispose()
        detached.dispose()
        assertEquals(1, fixture.stopCount.get())
        assertEquals("delivery.detached", await(detached.nextEvent(5_000.0)).asThirdOrNull()?.failure?.code)
        await(owner.close())
        assertTrue(detached.beginStop().isSecond)
        assertEquals(1, fixture.stopCount.get())
    }

    @Test
    fun retainedScanObservesRustOwnerClosedAndCannotStopReplacement() = withScanFixture { fixture ->
        await(owner.start())
        val old = acceptedScan()
        assertNull(await(old.waitUntilStarted(5_000.0)).failure)
        await(owner.close())
        assertEquals(BlengScanEndReason.OWNERCLOSED, await(old.nextEvent(5_000.0)).asSecondOrNull()?.ended?.reason)
        withScanFixture { replacement ->
            await(owner.start())
            val current = acceptedScan()
            assertNull(await(current.waitUntilStarted(5_000.0)).failure)
            assertTrue(old.beginStop().isSecond)
            assertEquals(0, replacement.stopCount.get())
            replacement.advertise()
            assertTrue(await(current.nextEvent(5_000.0)).isFirst)
        }
        assertEquals(0, fixture.stopCount.get())
    }

    @Test
    fun malformedProjectedDtosAndUnexpectedExceptionsFailClosedWithFixedMessage() {
        val error = ForeignScanFailure(ForeignScanFailureDomain.INPUT, "synthetic", "typed message", null)
        val fake = object : SupervisedScan(SupervisorNoHandle) {
            override suspend fun nextObservation(timeoutMs: Double) = ForeignScanObservation(null, null, null)
            override suspend fun startResult(timeoutMs: Double): ForeignScanCommandResult =
                throw IllegalStateException("secret-native-context")
            override suspend fun cancelStartResult(): ForeignScanCommandResult =
                throw IllegalStateException("secret-native-context")
            override fun beginStopView() = ForeignScanStopResult(null, null)
            override fun detach() = Unit
        }
        val scan = HybridBlengScan(fake)
        assertBridgeFailure(requireNotNull(await(scan.waitUntilStarted(1.0)).failure))
        assertBridgeFailure(requireNotNull(await(scan.cancelStart()).failure))
        assertBridgeFailure(requireNotNull(await(scan.nextEvent(1.0)).asThirdOrNull()).failure)
        assertBridgeFailure(requireNotNull(scan.beginStop().asSecondOrNull()).failure)

        for (dto in listOf(ForeignScanBeginResult(null, null), ForeignScanBeginResult(fake, error))) {
            assertBridgeFailure((mapScanDto("submission", dto) as BlengScanSubmission).asSecondOrNull()!!.failure)
        }
        val advertisement = ForeignScanAdvertisement(
            SupervisedPeripheral(SupervisorNoHandle), null, emptyList(), emptyList(), emptyList(), null, null,
        )
        val ended = ForeignScanEnd(ForeignScanEndReason.REQUESTED, null)
        for (dto in listOf(
            ForeignScanObservation(null, null, null),
            ForeignScanObservation(null, ended, error),
            ForeignScanObservation(advertisement, ended, null),
            ForeignScanObservation(advertisement, null, error),
            ForeignScanObservation(advertisement, ended, error),
        )) {
            assertBridgeFailure((mapScanDto("observation", dto) as BlengScanNext).asThirdOrNull()!!.failure)
        }
        val fakeStop = object : SupervisedScanStop(SupervisorNoHandle) {
            override suspend fun waitResult(timeoutMs: Double): ForeignScanCommandResult =
                throw IllegalStateException("secret-native-context")
            override suspend fun cancelResult(): ForeignScanCommandResult =
                throw IllegalStateException("secret-native-context")
        }
        val stop = HybridBlengScanStop(fakeStop)
        assertBridgeFailure(requireNotNull(await(stop.wait(1.0)).failure))
        assertBridgeFailure(requireNotNull(await(stop.cancel()).failure))
        for (dto in listOf(ForeignScanStopResult(null, null), ForeignScanStopResult(fakeStop, error))) {
            assertBridgeFailure((mapScanDto("stopSubmission", dto) as BlengScanStopSubmission).asSecondOrNull()!!.failure)
        }
        scan.dispose()
    }

    private fun mapScanDto(name: String, dto: Any): Any {
        val mapping = Class.forName("com.margelo.nitro.blehubreactnativebleng.BlengScanMapping")
        val instance = mapping.getField("INSTANCE").get(null)
        return mapping.getDeclaredMethod(name, dto.javaClass).apply {
            isAccessible = true
        }.invoke(instance, dto)
    }

    private fun assertBridgeFailure(failure: BlengScanFailureInfo) {
        assertEquals(BlengScanFailureDomain.DELIVERY, failure.domain)
        assertEquals("bridgeFailure", failure.code)
        assertEquals("Native scan bridge failed.", failure.message)
        assertNull(failure.platform)
    }

    private fun acceptedScan(): HybridBlengScanSpec = requireNotNull(owner.beginScan(scanOptions()).asFirstOrNull()).scan

    private fun scanOptions(company: Double? = null) = BlengScanOptions(emptyArray(), null, company, false)

    private fun rootOwnerField() = HybridBlengSessionOwner::class.java.getDeclaredField("owner").apply {
        isAccessible = true
    }

    private fun withScanFixture(
        autoInitialize: Boolean = true,
        rejectFirstInitialization: Boolean = false,
        reportRetirementLeak: Boolean = false,
        replacementFixture: ScanFixture? = null,
        action: (ScanFixture) -> Unit,
    ) {
        val fixture = ScanFixture(autoInitialize, rejectFirstInitialization, reportRetirementLeak)
        val factoryField = HybridBlengSessionOwner::class.java.getDeclaredField("ownerFactory").apply { isAccessible = true }
        val previousFactory = factoryField.get(owner)
        val factory: () -> AndroidRustSupervisorOwner = {
            AndroidRustSupervisorOwner(
                context = InstrumentationRegistry.getInstrumentation().targetContext,
                queueLabel = "BlengScanSyntheticReplacement",
                sessionFactory = replacementFixture ?: fixture,
            )
        }
        factoryField.set(owner, factory)
        HybridBlengSessionOwner::class.java.getDeclaredField("terminalClose").apply {
            isAccessible = true
            set(owner, null)
        }
        rootOwnerField().set(owner, AndroidRustSupervisorOwner(
            context = InstrumentationRegistry.getInstrumentation().targetContext,
            queueLabel = "BlengScanSynthetic",
            sessionFactory = fixture,
        ))
        var primary: Throwable? = null
        try {
            action(fixture)
        } catch (error: Throwable) {
            primary = error
            throw error
        } finally {
            try {
                fixture.completeInitialization()
                await(owner.close())
            } catch (cleanup: Throwable) {
                if (primary == null) throw cleanup
                primary.addSuppressed(cleanup)
            } finally {
                factoryField.set(owner, previousFactory)
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun testReactContext() = BridgeReactContext(
        InstrumentationRegistry.getInstrumentation().targetContext,
    )

    private fun <T> await(promise: Promise<T>): T = awaitSettlement(promise).getOrThrow()

    private fun <T> awaitSettlement(promise: Promise<T>): Result<T> {
        val settled = CountDownLatch(1)
        val result = AtomicReference<Result<T>>()
        promise.then {
            result.set(Result.success(it))
            settled.countDown()
        }.catch {
            result.set(Result.failure(it))
            settled.countDown()
        }
        assertTrue("Native Nitro Promise did not settle within 15s", settled.await(15, TimeUnit.SECONDS))
        return requireNotNull(result.get())
    }
}

/** Runs lifecycle launch blocks in an explicitly chosen order, without creating native sessions. */
private class QueuedLifecycleDispatcher : CoroutineDispatcher() {
    private val tasks = ArrayDeque<Runnable>()

    override fun dispatch(context: CoroutineContext, block: Runnable) = synchronized(tasks) {
        tasks.addLast(block)
    }

    fun runFirst() = synchronized(tasks) { tasks.removeFirst() }.run()

    fun runLast() = synchronized(tasks) { tasks.removeLast() }.run()
}

private const val SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"

/** Only the actual foreign backend boundary is synthetic; admission, validation and delivery are Rust. */
private class ScanFixture(
    private val autoInitialize: Boolean,
    private var rejectFirstInitialization: Boolean = false,
    private val reportRetirementLeak: Boolean = false,
) : ForeignPlatformOwnerSessionFactory {
    val disposeCount = AtomicInteger()
    val scanCount = AtomicInteger()
    val stopCount = AtomicInteger()
    val initializing = CompletableDeferred<InitializeCommand>()
    val manufacturerBytes = byteArrayOf(0, 127, -128, -1)

    @Volatile private var ingress: BackendEventIngressHandle? = null
    @Volatile private var initializeCommand: InitializeCommand? = null
    @Volatile private var scanCommand: StartScanCommand? = null
    private var reader: Deferred<Unit>? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val snapshot = AdapterSnapshot(
        AdapterState.POWERED_ON, AuthorizationState.AUTHORIZED, AuthorizationState.AUTHORIZED, "synthetic scan",
    )
    private val backend = Proxy.newProxyInstance(BleBackend::class.java.classLoader, arrayOf(BleBackend::class.java)) {
        _, method, args ->
        when (method.name) {
            "capabilities" -> BackendCapabilities(Platform.ANDROID, blehubContractVersion(), false, false, false, false, false, false)
            "bindEventIngress" -> { ingress = args!![0] as BackendEventIngressHandle; Unit }
            "initialize" -> {
                initializeCommand = args!![0] as InitializeCommand
                initializing.complete(requireNotNull(initializeCommand))
                if (rejectFirstInitialization) {
                    rejectFirstInitialization = false
                    initializeCommand = null
                    throw dev.blehub.CommandDispatchException.InvalidState("real startup rejection from fixture backend")
                }
                if (autoInitialize) completeInitialization()
                Unit
            }
            "startScan" -> {
                val command = args!![0] as StartScanCommand
                scanCommand = command
                scanCount.incrementAndGet()
                push(BleEvent.ScanStarted(command.commandId, command.scanSessionId))
                push(BleEvent.CommandCompleted(command.commandId, null))
                Unit
            }
            "stopScan" -> {
                val command = args!![0] as StopScanCommand
                stopCount.incrementAndGet()
                push(BleEvent.ScanStopped(command.scanSessionId, ScanStopResult.Ok(ScanStopReason.Requested(command.commandId))))
                push(BleEvent.CommandCompleted(command.commandId, null))
                Unit
            }
            "dispose" -> { disposeCount.incrementAndGet(); ingress = null; Unit }
            "toString" -> "SyntheticScanBackend"
            "hashCode" -> System.identityHashCode(this)
            "equals" -> false
            else -> error("Unexpected synthetic backend call: ${method.name}")
        }
    } as BleBackend

    override fun requiredZeroResourceNames() = listOf("ingressRoots")

    override fun create(context: ForeignOwnerContext): ForeignPlatformOwnerSession = object : ForeignPlatformOwnerSession {
        override fun context() = context.copy()
        override fun backend() = backend
        override fun disposeUnassembled() = backend.dispose()
        override fun startEventReader(owner: Bleng, scanProjection: ScanEventProjection) {
            reader = scope.async {
                try {
                    while (true) {
                        val event = owner.nextEvent(300_000uL) ?: break
                        scanProjection.project(event)
                    }
                } finally {
                    scanProjection.destroy()
                    owner.destroy()
                }
            }
        }
        override suspend fun waitEventReader() {
            requireNotNull(reader).await()

        }
        override suspend fun resourceSnapshot() = listOf(ForeignResourceCount(
            "ingressRoots", if (ingress == null && !reportRetirementLeak) 0uL else 1uL,
        ))
        override suspend fun executeRecoveryAction(sequence: ULong, action: ForeignPlatformRecoveryAction): ForeignPlatformActionResult =
            error("Unexpected recovery action")
    }

    fun completeInitialization() {
        initializeCommand?.let {
            initializeCommand = null
            push(BleEvent.AdapterSnapshotChanged(it.commandId, snapshot))
            push(BleEvent.CommandCompleted(it.commandId, null))
        }
    }

    fun advertise(localName: String? = null, rssi: Short? = null, connectable: Boolean? = null) {
        push(BleEvent.AdvertisementObserved(AdvertisementObservation(
            requireNotNull(scanCommand).scanSessionId,
            PeripheralId(987uL),
            localName,
            listOf(BluetoothUuid(SERVICE_UUID)),
            listOf(ManufacturerData(65535u.toUShort(), manufacturerBytes)),
            listOf(ServiceData(BluetoothUuid(SERVICE_UUID), byteArrayOf())),
            rssi,
            connectable,
        )))
    }

    fun endWithFailure() {
        push(BleEvent.ScanStopped(requireNotNull(scanCommand).scanSessionId, ScanStopResult.Err(BleEventError(
            BleErrorCategory.PERMISSION,
            "permission denied by synthetic backend",
            PlatformErrorDetails(Platform.ANDROID, "13", "Permission", "denied"),
        ))))
    }

    private fun push(event: BleEvent) {
        check(requireNotNull(ingress).push(event) == EventIngressStatus.Delivered)
    }
}
