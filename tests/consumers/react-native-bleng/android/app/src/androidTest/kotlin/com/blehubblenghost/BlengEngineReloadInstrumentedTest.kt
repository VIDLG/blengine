package com.blehubblenghost

import android.app.Application
import androidx.test.platform.app.InstrumentationRegistry
import com.blehub.reactnative.bleng.BlengEngineLifetime
import com.facebook.react.PackageList
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.defaults.DefaultReactHost
import com.facebook.react.interfaces.TaskInterface
import java.io.File
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Real-device delivery gate for the Android engine lifetime.
 *
 * Apple's equivalent proof is its native target creating a real `RCTHost`. This test does the same
 * on Android: it builds a real `ReactHost` in the instrumentation process, starts it, and reloads
 * it. It never uses a production reload trigger and never adds a test-only bypass.
 *
 * The specific unknown closed here is *delivery*: React Native's own teardown must reach
 * `BleHubBlengEngineLifecycleModule.invalidate()` for the engine being destroyed.
 */
class BlengEngineReloadInstrumentedTest {
    private companion object {
        const val HOST_TIMEOUT_SECONDS = 120L
        const val RETIREMENT_TIMEOUT_MS = 15_000L

        /**
         * Native-only lookup of the engine lifetime registered for an exact context.
         *
         * The accessor is `internal` by design, so this test reflects on it rather than widening a
         * production API. This mirrors how the existing instrumentation reaches other non-JS
         * internals.
         */
        private fun moduleClass(): Class<*> = Class.forName(
            "com.margelo.nitro.blehubreactnativebleng.BleHubBlengEngineLifecycleModule",
        )

        private fun moduleClassLoader(): ClassLoader? = moduleClass().classLoader

        private fun searchClasses(): List<Class<*>> = buildList {
            add(moduleClass())
            runCatching {
                add(
                    Class.forName(
                        "com.margelo.nitro.blehubreactnativebleng" +
                            ".BleHubBlengEngineLifecycleModule\$Companion",
                    ),
                )
            }
        }

        /** Kotlin internal members are name-mangled, so matching is by infix. */
        fun lifetimeFor(context: ReactApplicationContext?): BlengEngineLifetime? {
            for (type in searchClasses()) {
                for (method in type.declaredMethods) {
                    if (!method.name.contains("lifetimeFor") || method.parameterCount != 1) continue
                    method.isAccessible = true
                    val target = if (java.lang.reflect.Modifier.isStatic(method.modifiers)) null else
                        moduleClass().getDeclaredField("Companion")
                            .apply { isAccessible = true }
                            .get(null)
                    @Suppress("UNCHECKED_CAST")
                    return method.invoke(target, context) as BlengEngineLifetime?
                }
            }
            val seen = searchClasses().joinToString(";") { type ->
                type.simpleName + ":" + type.declaredMethods.joinToString(",") { it.name }
            }
            error("engine lifetime accessor is missing ($seen)")
        }

        /** Diagnostic only: how many engine lifetimes are registered right now. */
        fun registeredCount(): Int {
            return runCatching {
                val field = searchClasses().asSequence()
                    .flatMap { it.declaredFields.asSequence() }
                    .first { it.name.contains("lifetimes") }
                field.isAccessible = true
                val registry = field.get(null) ?: return 0
                val mapField = registry.javaClass.declaredFields
                    .first { java.util.Map::class.java.isAssignableFrom(it.type) }
                mapField.isAccessible = true
                val mapObj = mapField.get(registry) ?: return 0
                mapObj.javaClass.getMethod("size").invoke(mapObj) as Int
            }.getOrDefault(-1)
        }
    }

    @Test
    fun realHostReloadRetiresTheOldEngineBeforeRegisteringTheReplacement() {
        val application = InstrumentationRegistry.getInstrumentation()
            .targetContext.applicationContext as Application
        // A host needs a JS bundle; this probe needs no JavaScript behaviour, because the engine
        // module is created eagerly before JS runs.
        val bundle = File(application.filesDir, "blehub-engine-reload-probe.bundle").apply {
            writeText("// BleHub engine reload probe: intentionally empty.\n")
        }
        val host = DefaultReactHost.getDefaultReactHost(
            context = application,
            packageList = PackageList(application).packages,
            jsMainModulePath = "index",
            jsBundleFilePath = bundle.absolutePath,
            useDevSupport = false,
        )
        try {
            await("host start") { host.start() }
            val firstContext = host.currentReactContext as? ReactApplicationContext
            assertNotNull("A started host must expose its ReactApplicationContext", firstContext)
            // Do not call getNativeModule here: that would itself create the module and hide
            // whether eager initialisation delivered it. Poll the registry only.
            val first = waitForLifetime(firstContext)
            assertNotNull(
                "RN must create the engine module eagerly for a live engine; " +
                    "registered=${registeredCount()}, " +
                    "sameLoader=${moduleClassLoader() === application.javaClass.classLoader}",
                first,
            )
            assertTrue("A fresh engine must not be invalidated", first!!.isInvalidated.not())

            // The only production-visible signal: a reload must retire this engine.
            await("host reload") { host.reload("blehub-engine-parity") }
            assertTrue(
                "RN teardown did not reach the engine module",
                waitUntil { first.isInvalidated },
            )
            val receipt = runBlocking { first.waitForRetirement(RETIREMENT_TIMEOUT_MS) }
            assertTrue(
                "A retired engine with no bound owner must report no owner results",
                receipt.isEmpty(),
            )
            assertNull(
                "A retired engine must not stay discoverable for its old context",
                lifetimeFor(firstContext),
            )

            val secondContext = host.currentReactContext as? ReactApplicationContext
            assertNotNull("The replacement host must expose a new context", secondContext)
            val second = lifetimeFor(secondContext)
            assertNotNull("The replacement engine needs its own lifetime", second)
            assertNotSame("A reload must not reuse the retired lifetime", first, second)
            assertTrue("The replacement engine must be fresh", second!!.isInvalidated.not())
        } finally {
            runCatching { host.destroy("blehub-engine-parity-teardown", null) }
                .getOrNull()
                ?.let { task -> runCatching { task.waitForCompletion(HOST_TIMEOUT_SECONDS, TimeUnit.SECONDS) } }
            bundle.delete()
        }
    }

    private fun waitForLifetime(
        context: ReactApplicationContext?,
    ): com.blehub.reactnative.bleng.BlengEngineLifetime? {
        val deadline = System.currentTimeMillis() + RETIREMENT_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            lifetimeFor(context)?.let { return it }
            Thread.sleep(50)
        }
        return null
    }

    private fun await(label: String, block: () -> com.facebook.react.interfaces.TaskInterface<*>?) {
        val task = requireNotNull(block()) { "$label returned no task" }
        check(task.waitForCompletion(HOST_TIMEOUT_SECONDS, TimeUnit.SECONDS)) {
            "$label did not settle within ${HOST_TIMEOUT_SECONDS}s"
        }
        check(!task.isFaulted()) { "$label failed: ${task.getError()?.message}" }
    }

    private fun waitUntil(predicate: () -> Boolean): Boolean {
        val deadline = System.currentTimeMillis() + RETIREMENT_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            if (predicate()) return true
            Thread.sleep(50)
        }
        return false
    }
}
