package com.margelo.nitro.blehubreactnativebleng

import com.facebook.react.BaseReactPackage
import com.facebook.react.bridge.NativeModule
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.module.model.ReactModuleInfoProvider

/** RN autolinking entry point; Nitro owns the actual HybridObject registration. */
class BleHubReactNativeBlengPackage : BaseReactPackage() {
    override fun getModule(name: String, reactContext: ReactApplicationContext): NativeModule? = null

    override fun getReactModuleInfoProvider(): ReactModuleInfoProvider =
        ReactModuleInfoProvider { emptyMap() }

    companion object {
        init {
            BleHubReactNativeBlengOnLoad.initializeNative()
        }
    }
}
