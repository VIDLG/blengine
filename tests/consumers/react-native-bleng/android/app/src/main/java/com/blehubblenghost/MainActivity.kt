package com.blehubblenghost

import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate

class MainActivity : ReactActivity() {
    override fun getMainComponentName(): String = "BleHubBlengHost"

    override fun createReactActivityDelegate(): ReactActivityDelegate =
        DefaultReactActivityDelegate(this, mainComponentName, fabricEnabled)

    /**
     * The App is leaving the screen: hold the process so an active BLE session is not killed.
     *
     * Scoping the service to the background is the whole point — the user sees the
     * notification only while the App is away, and returning to the App removes it. The
     * service owns no BLE state; it only denies the OS the chance to reclaim the process.
     */
    override fun onStop() {
        BleHubKeepAliveService.start(this)
        super.onStop()
    }

    override fun onStart() {
        super.onStart()
        BleHubKeepAliveService.stop(this)
    }
}
