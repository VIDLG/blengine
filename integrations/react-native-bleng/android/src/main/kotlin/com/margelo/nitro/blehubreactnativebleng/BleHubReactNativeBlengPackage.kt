package com.margelo.nitro.blehubreactnativebleng

import com.blehub.reactnative.bleng.BlengEngineHostWiring
import com.facebook.react.BaseReactPackage
import com.facebook.react.bridge.NativeModule
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.module.model.ReactModuleInfo
import com.facebook.react.module.model.ReactModuleInfoProvider

/**
 * RN autolinking entry point; Nitro owns the HybridObject registration.
 *
 * The engine lifecycle module is the only RN module here. It has no JavaScript surface: RN creates
 * it eagerly per engine so a session owner can bind to its engine before any native work starts.
 *
 * This package instance is one host's engine-linkage scope, mirroring the Apple delegate: it
 * survives engine reloads, while [getModule] runs once per new engine and installs the exact
 * predecessor there. No process-global current-engine state exists.
 */
class BleHubReactNativeBlengPackage : BaseReactPackage() {
    private val engineWiring = BlengEngineHostWiring()

    override fun getModule(name: String, reactContext: ReactApplicationContext): NativeModule? =
        when (name) {
            BleHubBlengEngineLifecycleModule.NAME ->
                BleHubBlengEngineLifecycleModule(reactContext, engineWiring)
            else -> null
        }

    override fun getReactModuleInfoProvider(): ReactModuleInfoProvider =
        ReactModuleInfoProvider {
            mapOf(
                BleHubBlengEngineLifecycleModule.NAME to
                    ReactModuleInfo(
                        BleHubBlengEngineLifecycleModule.NAME,
                        BleHubBlengEngineLifecycleModule::class.java.name,
                        false, // canOverrideExistingModule
                        true, // needsEagerInit: registration must precede JS owner creation
                        false, // isCxxModule
                        true, // isTurboModule: TurboModuleManager owns invalidate() delivery
                    )
            )
        }

    companion object {
        init {
            BleHubReactNativeBlengOnLoad.initializeNative()
        }
    }
}
