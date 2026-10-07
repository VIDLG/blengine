package com.blehubnitrohost

import android.content.Intent
import android.os.Bundle
import android.util.Log
import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate

class MainActivity : ReactActivity() {
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (!intent.getBooleanExtra(RELOAD_EXTRA, false)) return
        intent.removeExtra(RELOAD_EXTRA)
        runOnUiThread {
            Log.i(TAG, "Android RN JS runtime reload requested")
            getReactDelegate()?.reload()
        }
    }

    /**
     * Returns the name of the main component registered from JavaScript. This is used to schedule
     * rendering of the component.
     */
    override fun getMainComponentName(): String = "BleHubNitroHost"

    /**
     * Returns the instance of the [ReactActivityDelegate]. We use [DefaultReactActivityDelegate]
     * which allows you to enable New Architecture with a single boolean flags [fabricEnabled]
     */
    override fun createReactActivityDelegate(): ReactActivityDelegate =
        object : DefaultReactActivityDelegate(this, mainComponentName, fabricEnabled) {
            override fun getLaunchOptions(): Bundle = Bundle().apply {
                putString("runtimeMode", intent.getStringExtra("blehub.runtimeMode") ?: "basic")
                putString("rfServiceUuid", intent.getStringExtra("blehub.rfServiceUuid") ?: "")
                putString("rfMultiNotificationMode", intent.getStringExtra("blehub.rfMultiNotificationMode") ?: "both")
                putString(
                    "rfMultiSubscriptionMode",
                    intent.getStringExtra("blehub.rfMultiSubscriptionMode") ?: "notification"
                )
                putString(
                    "rfMultiCycles",
                    intent.getStringExtra("blehub.rfMultiCycles") ?: "10"
                )
                putString(
                    "rfReloadNotificationName",
                    intent.getStringExtra("blehub.rfReloadNotificationName") ?: "",
                )
            }
        }

    private companion object {
        const val RELOAD_EXTRA = "blehub.triggerReload"
        const val TAG = "BleHubNitroHost"
    }
}
