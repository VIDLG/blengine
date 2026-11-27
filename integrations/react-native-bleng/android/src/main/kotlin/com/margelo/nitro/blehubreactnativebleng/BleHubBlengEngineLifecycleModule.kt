package com.margelo.nitro.blehubreactnativebleng

import com.blehub.reactnative.bleng.BlengEngineHostWiring
import com.blehub.reactnative.bleng.BlengEngineLifetime
import com.blehub.reactnative.bleng.BlengEngineLifetimeRegistry
import com.facebook.react.bridge.BaseJavaModule
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.module.annotations.ReactModule
import com.facebook.react.turbomodule.core.interfaces.TurboModule

/**
 * Native-only Android engine boundary for the formal BLeng package line.
 *
 * React Native creates one instance per `ReactApplicationContext`, and
 * `TurboModuleManager` calls [invalidate] when that engine is torn down. The module therefore owns
 * exactly one engine's native membership.
 *
 * It deliberately exposes **no** JavaScript method: it is a lifecycle participant, not an API. The
 * engine lifetime is discoverable only through the exact `ReactApplicationContext` that created it,
 * so nothing infers a "current engine" from process-global state.
 */
@ReactModule(name = BleHubBlengEngineLifecycleModule.NAME)
class BleHubBlengEngineLifecycleModule(
    private val context: ReactApplicationContext,
    wiring: BlengEngineHostWiring,
) : BaseJavaModule(), TurboModule {
    private val lifetime = BlengEngineLifetime()

    init {
        // Never throw from module creation: RN could strand its module holder in "creating".
        // A refused registration or refused predecessor link seals admission instead, so owners
        // fail closed rather than starting without old-engine retirement.
        if (!lifetimes.register(context, lifetime) || !wiring.attach(lifetime)) {
            lifetime.rejectHostAdmission()
        }
    }

    override fun getName(): String = NAME

    /** RN engine teardown. Retirement is idempotent and never blocks this callback. */
    override fun invalidate() {
        lifetimes.unregister(context, lifetime)
        lifetime.invalidate()
    }

    companion object {
        const val NAME = "BleHubBlengEngineLifecycle"

        private val lifetimes = BlengEngineLifetimeRegistry<ReactApplicationContext>()

        /** Exact-context lookup only; `null` means "no engine lifetime", never "use a default". */
        internal fun lifetimeFor(context: ReactApplicationContext?): BlengEngineLifetime? =
            lifetimes.forKey(context)
    }
}
