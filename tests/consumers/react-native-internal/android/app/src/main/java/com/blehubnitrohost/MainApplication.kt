package com.blehubnitrohost

import android.app.Application
import com.facebook.react.PackageList
import com.facebook.react.ReactApplication
import com.facebook.react.ReactHost
import com.facebook.react.ReactNativeApplicationEntryPoint.loadReactNative
import com.facebook.react.defaults.DefaultReactHost.getDefaultReactHost
import com.facebook.react.bridge.ReactApplicationContext
import com.margelo.nitro.blehubreactnative.BleHubProbeLifecycleOwners
import com.margelo.nitro.blehubreactnative.PendingReadReloadSession

class MainApplication : Application(), ReactApplication {

  override val reactHost: ReactHost by lazy {
    getDefaultReactHost(
      context = applicationContext,
      packageList =
        PackageList(this).packages.apply {
          // Packages that cannot be autolinked yet can be added manually here, for example:
          // add(MyReactNativePackage())
        },
    ).also { host ->
      host.addBeforeDestroyListener {
        (host.currentReactContext as? ReactApplicationContext)?.let { context ->
          BleHubProbeLifecycleOwners.retireEngine(context)
        }
        pendingSession?.capture()?.takeIf { it.generation == 2 }?.let {
          pendingSurfaceUpdate?.invoke(it.generation)
        }
      }
    }
  }

  private var pendingSession: PendingReadReloadSession? = null
  private var pendingSurfaceUpdate: ((Int) -> Unit)? = null

  fun configurePendingReload(session: PendingReadReloadSession, updateSurface: (Int) -> Unit) {
    check(pendingSession == null || pendingSession === session) { "pending host run cannot be replaced" }
    pendingSession = session
    pendingSurfaceUpdate = updateSurface
    BleHubProbeLifecycleOwners.configurePendingHost(reactHost, session)
  }

  fun detachPendingSurface() {
    pendingSurfaceUpdate = null
  }

  override fun onCreate() {
    super.onCreate()
    loadReactNative(this)
  }
}
