package com.blehubnitrohost

import android.content.Intent
import android.os.Bundle
import android.os.Process
import android.os.SystemClock
import android.util.Log
import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate
import com.facebook.react.runtime.ReactSurfaceImpl
import com.margelo.nitro.blehubreactnative.PendingReadReloadSession
import java.util.UUID
import org.json.JSONObject

class MainActivity : ReactActivity() {
    private var initialProps: Bundle? = null
    private var pendingSession: PendingReadReloadSession? = null
    private var pendingSurface: ReactSurfaceImpl? = null
    private val recoveryBootId = UUID.randomUUID().toString()
    private var recoveryRunId: String? = null
    private var recoveryNextCycle = 0
    private var recoveryFailed = false
    private var fullRecovery: FullRecoverySignalGate? = null
    private var writeReject: WriteRejectSignalGate? = null
    private var cccEnableReject: WriteRejectSignalGate? = null
    private var cccDisableReject: WriteRejectSignalGate? = null
    private var permissionRunId: String? = null
    private var permissionPhase: String? = null
    private var permissionArmed = false
    private var permissionFailed = false
    private val permissionStartedUnixMs = System.currentTimeMillis()
    private val permissionStartedElapsedMs = SystemClock.elapsedRealtime()
    private var adapterRunId: String? = null
    private var adapterMode = "startup-off"
    private var adapterExplicitMode = false
    private var adapterArmed = false
    private var adapterSignalled = false
    private var adapterFailed = false

    private fun failPermission(reason: String) {
        permissionFailed = true
        Log.e(TAG, "BLEHUB_NITRO_RUNTIME rf-permission-transition:failed:phase=$permissionPhase:runId=$permissionRunId:native:$reason")
    }

    private fun permissionRecord(kind: String, url: String? = null) {
        val record = JSONObject().apply {
            put("run_id", requireNotNull(permissionRunId))
            put("phase", requireNotNull(permissionPhase))
            put("pid", Process.myPid())
            put("uid", Process.myUid())
            // Android 36 UserHandle.getUserId(uid) uses this UID partition; that API is hidden.
            put("user_id", Process.myUid() / 100_000)
            put("boot_id", recoveryBootId)
            put("bundle_id", packageName)
            put("executable", java.io.File(applicationInfo.sourceDir).toURI().toString())
            put("source", if (url == null) "launch" else "payload-url")
            put("started_unix_ms", permissionStartedUnixMs)
            put("started_elapsed_ms", permissionStartedElapsedMs)
            if (url != null) put("url", url)
        }
        Log.i(TAG, "BLEHUB_NITRO_RUNTIME rf-permission-transition:native:$kind:$record")
    }

    private fun failAdapter(reason: String) {
        adapterFailed = true
        Log.e(TAG, "BLEHUB_NITRO_RUNTIME rf-adapter-transition:failed:native:$reason")
    }

    private fun adapterRecord(kind: String, url: String? = null) {
        val record = JSONObject().apply {
            put("run_id", requireNotNull(adapterRunId))
            if (adapterExplicitMode) put("adapter_mode", adapterMode)
            put("pid", Process.myPid())
            put("boot_id", recoveryBootId)
            put("bundle_id", packageName)
            put("executable", java.io.File(applicationInfo.sourceDir).toURI().toString())
            put("source", if (url == null) "launch" else "payload-url")
            if (url != null) put("url", url)
        }
        Log.i(TAG, "BLEHUB_NITRO_RUNTIME rf-adapter-transition:native:$kind:$record")
    }

    private fun failFull(reason: String) {
        recoveryFailed = true
        Log.e(TAG, "BLEHUB_NITRO_RUNTIME rf-lab-full:failed:native:$reason")
    }

    private fun recoveryRecord(kind: String, cycle: Int? = null, url: String? = null) {
        val record = JSONObject().apply {
            put("run_id", requireNotNull(recoveryRunId))
            put("pid", Process.myPid())
            put("boot_id", recoveryBootId)
            put("bundle_id", packageName)
            put("executable", java.io.File(applicationInfo.sourceDir).toURI().toString())
            put("source", if (url == null) "launch" else "payload-url")
            if (cycle != null) put("cycle", cycle)
            if (url != null) put("url", url)
        }
        val prefix = if (kind == "begin") "rf-lab-full:native:signal" else "rf-recovery-ten:native:$kind"
        Log.i(TAG, "BLEHUB_NITRO_RUNTIME $prefix:$record")
    }

    override fun onNewIntent(intent: Intent) {
        (cccDisableReject ?: cccEnableReject ?: writeReject)?.let { gate ->
            if (intent.action != Intent.ACTION_VIEW || intent.getBooleanExtra(RELOAD_EXTRA, false) ||
                !gate.accept(intent.dataString)) {
                gate.fail()
                Log.e(TAG, "BLEHUB_NITRO_RUNTIME ${gate.mode}:failed:native:foreign-early-duplicate-url-or-reload")
                return
            }
            Log.i(TAG, "BLEHUB_NITRO_RUNTIME rf-host:signal:${gate.record(intent.dataString, recoveryBootId, packageName, applicationInfo.sourceDir)}")
            super.onNewIntent(intent)
            return
        }
        val permission = permissionRunId
        if (permission != null) {
            val url = intent.dataString
            if (permissionFailed || permissionPhase != "active" || permissionArmed ||
                intent.action != Intent.ACTION_VIEW ||
                url != "blehub-rn-spike://permission/$permission/active/arm" ||
                intent.getBooleanExtra(RELOAD_EXTRA, false)) {
                failPermission("foreign-or-duplicate-payload-url-or-reload")
                return
            }
            permissionArmed = true
            // Identity is recorded before Linking. Never replace the launch Intent/initial props.
            permissionRecord("signal", requireNotNull(url))
            super.onNewIntent(intent)
            return
        }
        val adapter = adapterRunId
        if (adapter != null) {
            val url = intent.dataString
            val arm = adapterMode == "active-loss" && !adapterArmed &&
                url == "blehub-rn-spike://adapter/$adapter/active-loss/arm"
            val onUrl = if (adapterMode == "active-loss")
                "blehub-rn-spike://adapter/$adapter/active-loss/on" else "blehub-rn-spike://adapter/$adapter/on"
            if (adapterFailed || adapterSignalled || intent.action != Intent.ACTION_VIEW ||
                (!arm && (url != onUrl || adapterMode == "active-loss" && !adapterArmed)) ||
                intent.getBooleanExtra(RELOAD_EXTRA, false)) {
                failAdapter("foreign-or-duplicate-payload-url-or-reload")
                return
            }
            if (arm) adapterArmed = true else adapterSignalled = true
            // JS validates phase readiness; record identity before Linking delivery.
            adapterRecord(if (arm) "arm" else "signal", requireNotNull(url))
            super.onNewIntent(intent)
            return
        }
        val full = fullRecovery
        if (full != null) {
            val url = intent.dataString
            if (recoveryFailed || intent.action != Intent.ACTION_VIEW || url == null ||
                intent.getBooleanExtra(RELOAD_EXTRA, false)) {
                failFull("invalid-intent")
                return
            }
            when (full.accept(url)) {
                FullRecoverySignalGate.Signal.REJECTED -> { failFull("foreign-early-or-duplicate-payload-url"); return }
                FullRecoverySignalGate.Signal.CONTROL -> {
                    Log.i(TAG, "BLEHUB_NITRO_RUNTIME rf-host:signal:${full.controlRecord(url, recoveryBootId, packageName, applicationInfo.sourceDir)}")
                }
                FullRecoverySignalGate.Signal.BEGIN -> recoveryRecord("begin", url = url)
                FullRecoverySignalGate.Signal.CYCLE -> {
                    recoveryRecord("signal", recoveryNextCycle, url)
                    recoveryNextCycle += 1
                }
            }
            super.onNewIntent(intent)
            return
        }
        val runId = recoveryRunId
        if (runId != null) {
            val expected = "blehub-rn-spike://recover/$runId/$recoveryNextCycle"
            if (recoveryFailed || recoveryNextCycle >= 10 || intent.action != Intent.ACTION_VIEW ||
                intent.dataString != expected || intent.getBooleanExtra(RELOAD_EXTRA, false)) {
                recoveryFailed = true
                Log.e(TAG, "BLEHUB_NITRO_RUNTIME rf-recovery-ten:native:failed:foreign-or-duplicate-payload-url")
                return
            }
            // Record installed/process identity before RN Linking delivery. Never replace launch props.
            recoveryRecord("signal", recoveryNextCycle, requireNotNull(intent.dataString))
            recoveryNextCycle += 1
            super.onNewIntent(intent)
            return
        }
        // Keep RN Linking propagation, including explicit-component ACTION_VIEW arm signals.
        // Do not replace the launch Intent with a control Intent lacking the initial RF props.
        super.onNewIntent(intent)
        if (!intent.getBooleanExtra(RELOAD_EXTRA, false)) return
        val session = pendingSession
        if (session != null && !session.requestReload(intent.getStringExtra(RUN_ID_EXTRA) ?: "")) {
            Log.e(TAG, "BLEHUB_NITRO_RUNTIME rf-pending-read-reload:native:native-boundary-failed:" +
                "runId=${session.runId}:reason=reload-intent-mismatch-or-duplicate")
            return
        }
        intent.removeExtra(RELOAD_EXTRA)
        runOnUiThread {
            Log.i(TAG, "Android RN JS runtime reload requested")
            getReactDelegate()?.reload()
        }
    }

    override fun onDestroy() {
        writeReject?.fail()
        cccEnableReject?.fail()
        cccDisableReject?.fail()
        if (pendingSession != null) (application as MainApplication).detachPendingSurface()
        super.onDestroy()
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
            override fun loadApp(appKey: String?) {
                val props = getLaunchOptions()
                if (pendingSession != null && pendingSurface == null) {
                    val app = application as MainApplication
                    val surface = app.reactHost.createSurface(this@MainActivity, requireNotNull(appKey), props)
                    pendingSurface = surface as ReactSurfaceImpl
                    setReactSurface(surface)
                    app.configurePendingReload(requireNotNull(pendingSession)) { generation ->
                        // RN has stopped the old surface before this callback. Reload restarts
                        // this SAME surface; getLaunchOptions/setIntent alone cannot update it.
                        val updated = Bundle(requireNotNull(initialProps)).apply {
                            putDouble("rfPendingReadGeneration", generation.toDouble())
                        }
                        requireNotNull(pendingSurface).updateInitProps(updated)
                    }
                }
                super.loadApp(appKey)
            }

            override fun getLaunchOptions(): Bundle {
                initialProps?.let { return Bundle(it) }
                val props = Bundle().apply {
                    putString("runtimeMode", intent.getStringExtra("blehub.runtimeMode") ?: "rf-workbench")
                    putString("rfServiceUuid", intent.getStringExtra("blehub.rfServiceUuid") ?: "")
                    intent.getStringExtra("blehub.rfExpectedLocalName")?.let {
                        putString("rfExpectedLocalName", it)
                    }
                    putString("rfLabSuiteFlow", intent.getStringExtra("blehub.rfLabSuiteFlow") ?: "")
                    putString("rfCancellationControl", intent.getStringExtra("blehub.rfCancellationControl") ?: "")
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
                val writeRejectMode = props.getString("runtimeMode") == "rf-write-reject"
                val writeRejectRaw = intent.getStringExtra("blehub.rfWriteRejectControl")
                require(writeRejectMode == (writeRejectRaw != null)) { "write reject control/mode mismatch" }
                require(!writeRejectMode || (!intent.hasExtra("blehub.rfCccEnableRejectControl") && !intent.hasExtra("blehub.rfCccDisableRejectControl"))) { "conflicting reject controls" }
                if (writeRejectMode) {
                    check(!writeRejectProcessLaunched) { "write reject cannot restart its Activity/engine in the same process" }
                    require(intent.dataString == null && !intent.getBooleanExtra(RELOAD_EXTRA, false) &&
                        !intent.hasExtra("blehub.rfRecoveryRunId") && !intent.hasExtra(RUN_ID_EXTRA) &&
                        !intent.hasExtra("blehub.rfAdapterRunId") && !intent.hasExtra("blehub.rfPermissionRunId") &&
                        props.getString("rfCancellationControl").isNullOrEmpty() && props.getString("rfLabSuiteFlow").isNullOrEmpty()) {
                        "write reject must be an exclusive standalone launch"
                    }
                    require(Regex("[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}").matches(props.getString("rfServiceUuid") ?: "") &&
                        !props.getString("rfExpectedLocalName").isNullOrBlank()) { "write reject fixture identity required" }
                    val gate = WriteRejectSignalGate(requireNotNull(writeRejectRaw))
                    writeReject = gate
                    writeRejectProcessLaunched = true
                    props.putString("rfWriteRejectControl", writeRejectRaw)
                    Log.i(TAG, "BLEHUB_NITRO_RUNTIME rf-host:bootstrap:${gate.record(null, recoveryBootId, packageName, applicationInfo.sourceDir)}")
                }
                val cccEnableRejectMode = props.getString("runtimeMode") == "rf-ccc-enable-reject"
                val cccEnableRejectRaw = intent.getStringExtra("blehub.rfCccEnableRejectControl")
                require(cccEnableRejectMode == (cccEnableRejectRaw != null)) { "CCC enable reject control/mode mismatch" }
                if (cccEnableRejectMode) {
                    check(!writeRejectProcessLaunched) { "CCC enable reject cannot restart its Activity/engine in the same process" }
                    require(intent.dataString == null && !intent.getBooleanExtra(RELOAD_EXTRA, false) &&
                        !intent.hasExtra("blehub.rfRecoveryRunId") && !intent.hasExtra(RUN_ID_EXTRA) &&
                        !intent.hasExtra("blehub.rfAdapterRunId") && !intent.hasExtra("blehub.rfPermissionRunId") &&
                        props.getString("rfCancellationControl").isNullOrEmpty() && props.getString("rfLabSuiteFlow").isNullOrEmpty()) {
                        "CCC enable reject must be an exclusive standalone launch"
                    }
                    require(Regex("[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}").matches(props.getString("rfServiceUuid") ?: "") &&
                        !props.getString("rfExpectedLocalName").isNullOrBlank()) { "CCC enable reject fixture identity required" }
                    require(!intent.hasExtra("blehub.rfWriteRejectControl") && !intent.hasExtra("blehub.rfCccDisableRejectControl")) { "conflicting reject controls" }
                    val gate = WriteRejectSignalGate(requireNotNull(cccEnableRejectRaw), "rf-ccc-enable-reject")
                    cccEnableReject = gate
                    writeRejectProcessLaunched = true
                    props.putString("rfCccEnableRejectControl", cccEnableRejectRaw)
                    Log.i(TAG, "BLEHUB_NITRO_RUNTIME rf-host:bootstrap:${gate.record(null, recoveryBootId, packageName, applicationInfo.sourceDir)}")
                }
                val cccDisableRejectMode = props.getString("runtimeMode") == "rf-ccc-disable-reject"
                val cccDisableRejectRaw = intent.getStringExtra("blehub.rfCccDisableRejectControl")
                require(cccDisableRejectMode == (cccDisableRejectRaw != null)) { "CCC disable reject control/mode mismatch" }
                if (cccDisableRejectMode) {
                    check(!writeRejectProcessLaunched) { "CCC disable reject cannot restart its Activity/engine in the same process" }
                    require(intent.dataString == null && !intent.getBooleanExtra(RELOAD_EXTRA, false) &&
                        !intent.hasExtra("blehub.rfRecoveryRunId") && !intent.hasExtra(RUN_ID_EXTRA) &&
                        !intent.hasExtra("blehub.rfAdapterRunId") && !intent.hasExtra("blehub.rfPermissionRunId") &&
                        props.getString("rfCancellationControl").isNullOrEmpty() && props.getString("rfLabSuiteFlow").isNullOrEmpty() &&
                        !intent.hasExtra("blehub.rfWriteRejectControl") && !intent.hasExtra("blehub.rfCccEnableRejectControl")) {
                        "CCC disable reject must be an exclusive standalone launch"
                    }
                    require(Regex("[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}").matches(props.getString("rfServiceUuid") ?: "")) {
                        "CCC disable reject fixture service required"
                    }
                    val name = props.getString("rfExpectedLocalName")
                    require(name == null || (name.isNotBlank() && name.length <= 128)) { "invalid fixture name" }
                    val gate = WriteRejectSignalGate(requireNotNull(cccDisableRejectRaw), "rf-ccc-disable-reject")
                    cccDisableReject = gate
                    writeRejectProcessLaunched = true
                    props.putString("rfCccDisableRejectControl", cccDisableRejectRaw)
                    Log.i(TAG, "BLEHUB_NITRO_RUNTIME rf-host:bootstrap:${gate.record(null, recoveryBootId, packageName, applicationInfo.sourceDir)}")
                }
                if (props.getString("runtimeMode") == "rf-pending-read-reload") {
                    val runId = requireNotNull(intent.getStringExtra(RUN_ID_EXTRA)) {
                        "pending read mode requires blehub.rfPendingReadRunId"
                    }
                    val session = PendingReadReloadSession(runId)
                    require(props.getString("rfReloadNotificationName") == "dev.blehub.rn.reload.$runId") {
                        "pending read reload notification does not match runId"
                    }
                    pendingSession = session
                    props.putString("rfPendingReadRunId", runId)
                    props.putDouble("rfPendingReadGeneration", 1.0)
                }
                if (props.getString("runtimeMode") == "rf-permission-transition") {
                    val runId = intent.getStringExtra("blehub.rfPermissionRunId")
                    val phase = intent.getStringExtra("blehub.rfPermissionPhase")
                    if (permissionProcessLaunched || runId == null ||
                        !Regex("[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}").matches(runId) ||
                        UUID.fromString(runId).toString() != runId ||
                        phase !in listOf("denied", "active", "restored") || intent.dataString != null ||
                        intent.getBooleanExtra(RELOAD_EXTRA, false)) {
                        failPermission("invalid-launch-or-nonfresh-process")
                        error("permission transition requires a fresh App process, canonical launch runId/phase and no URL/reload")
                    }
                    permissionProcessLaunched = true
                    permissionRunId = runId
                    permissionPhase = phase
                    props.putString("rfPermissionRunId", runId)
                    props.putString("rfPermissionPhase", phase)
                    permissionRecord("bootstrap")
                }
                if (props.getString("runtimeMode") == "rf-adapter-transition") {
                    val runId = intent.getStringExtra("blehub.rfAdapterRunId")
                    if (runId == null ||
                        !Regex("[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}").matches(runId) ||
                        UUID.fromString(runId).toString() != runId || intent.dataString != null ||
                        intent.getBooleanExtra(RELOAD_EXTRA, false)) {
                        failAdapter("invalid-launch-run-id-or-payload")
                        error("adapter transition requires a canonical launch runId and no URL/reload")
                    }
                    adapterExplicitMode = intent.hasExtra("blehub.rfAdapterMode")
                    adapterMode = intent.getStringExtra("blehub.rfAdapterMode") ?: "startup-off"
                    require(adapterMode == "startup-off" || adapterMode == "active-loss") { "unknown adapter mode" }
                    adapterRunId = runId
                    props.putString("rfAdapterRunId", runId)
                    props.putString("rfAdapterMode", adapterMode)
                    adapterRecord("bootstrap")
                }
                val fullRun = props.getString("runtimeMode") == "rf-lab-suite" &&
                    intent.hasExtra("blehub.rfRecoveryRunId")
                if (props.getString("runtimeMode") == "rf-recovery-ten" || fullRun) {
                    val runId = requireNotNull(intent.getStringExtra("blehub.rfRecoveryRunId")) {
                        "strict recovery requires blehub.rfRecoveryRunId"
                    }
                    require(Regex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}").matches(runId) &&
                        UUID.fromString(runId).toString() == runId) { "noncanonical recovery runId" }
                    if (fullRun) {
                        require(props.getString("rfLabSuiteFlow").isNullOrEmpty()) { "full suite cannot select a flow" }
                        val gate = FullRecoverySignalGate(runId, requireNotNull(props.getString("rfCancellationControl")))
                        fullRecovery = gate
                        Log.i(TAG, "BLEHUB_NITRO_RUNTIME rf-host:bootstrap:${gate.controlRecord(null, recoveryBootId, packageName, applicationInfo.sourceDir)}")
                    }
                    recoveryRunId = runId
                    props.putString("rfRecoveryRunId", runId)
                    recoveryRecord("bootstrap")
                }
                initialProps = Bundle(props)
                return props
            }
        }

    private companion object {
        var permissionProcessLaunched = false
        var writeRejectProcessLaunched = false
        const val RUN_ID_EXTRA = "blehub.rfPendingReadRunId"
        const val RELOAD_EXTRA = "blehub.triggerReload"
        const val TAG = "BleHubNitroHost"
    }
}

/** Installed-host URL ledger. JS still enforces listener/readiness and real RF outcomes. */
internal class FullRecoverySignalGate(private val runId: String, raw: String) {
    enum class Signal { CONTROL, BEGIN, CYCLE, REJECTED }
    private val controls: List<JSONObject>
    private val urls: List<String>
    private var nextControl = 0
    private var begun = false
    private var nextCycle = 0
    private var failed = false

    init {
        require(Regex("[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}").matches(runId))
        val bundle = JSONObject(raw)
        require(bundle.keys().asSequence().toSet() == setOf("controls"))
        val values = bundle.getJSONArray("controls")
        require(values.length() == 2)
        controls = (0..1).map { index ->
            values.getJSONObject(index).also { value ->
                require(value.keys().asSequence().toSet() == setOf("session", "flow", "mode", "token"))
                require(value.getString("session") == runId && value.get("flow") is Int && value.getInt("flow") == index + 8 &&
                    value.getString("mode") == (if (index == 0) "rf-cancellation" else "rf-watchdog"))
                val token = value.getString("token")
                require(Regex("[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}").matches(token))
            }
        }
        require(controls[0].getString("token") != controls[1].getString("token"))
        urls = controls.flatMap { value ->
            val phases = if (value.getInt("flow") == 8) listOf("armed", "held", "released") else listOf("armed", "held", "released", "recovery")
            phases.map { phase -> "blehub-rf://control/$runId/${value.getInt("flow")}/${value.getString("mode")}/${value.getString("token")}/$phase" }
        }
    }

    fun accept(url: String): Signal {
        if (failed) return Signal.REJECTED
        val result = when {
            !begun && nextControl < urls.size && url == urls[nextControl] -> { nextControl += 1; Signal.CONTROL }
            !begun && nextControl == urls.size && url == "blehub-rn-spike://recover/$runId/begin" -> { begun = true; Signal.BEGIN }
            begun && nextCycle < 10 && url == "blehub-rn-spike://recover/$runId/$nextCycle" -> { nextCycle += 1; Signal.CYCLE }
            else -> Signal.REJECTED
        }
        if (result == Signal.REJECTED) failed = true
        return result
    }

    fun controlRecord(url: String?, bootId: String, bundleId: String, executable: String): JSONObject {
        val control = if (url == null || nextControl <= 3) controls[0] else controls[1]
        return JSONObject(control.toString()).apply {
            put("pid", Process.myPid()); put("boot_id", bootId); put("bundle_id", bundleId)
            put("executable", java.io.File(executable).toURI().toString())
            if (url != null) put("phase", url.substringAfterLast('/'))
        }
    }
}

/** Standalone native URL order/identity. JS additionally enforces request readiness before dispatch. */
internal class WriteRejectSignalGate(raw: String, val mode: String = "rf-write-reject") {
    private val flow = if (mode == "rf-ccc-disable-reject") 12 else if (mode == "rf-ccc-enable-reject") 11 else 10
    private val control = JSONObject(raw)
    private val phases = if (mode == "rf-ccc-disable-reject")
        listOf("baseline-value-sent", "armed", "held", "rejected", "continued-value-sent", "retry-allowed", "silence-started", "close-allowed")
        else if (mode == "rf-ccc-enable-reject")
        listOf("armed", "held", "rejected", "value-sent", "disable-allowed", "silence-started", "close-allowed")
        else listOf("armed", "held", "rejected", "close-allowed")
    private var next = 0
    private var failed = false

    init {
        require(control.keys().asSequence().toSet() == setOf("session", "flow", "mode", "token"))
        require(mode == "rf-write-reject" || mode == "rf-ccc-enable-reject" || mode == "rf-ccc-disable-reject")
        require(control.get("flow") is Int && control.getInt("flow") == flow && control.getString("mode") == mode)
        for (key in listOf("session", "token")) {
            val value = control.getString(key)
            require(Regex("[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}").matches(value) && UUID.fromString(value).toString() == value)
        }
    }

    fun fail() { failed = true }

    fun accept(url: String?): Boolean {
        val prefix = "blehub-rf://control/${control.getString("session")}/$flow/$mode/${control.getString("token")}/"
        if (failed || next >= phases.size || url != prefix + phases[next]) { fail(); return false }
        next += 1
        return true
    }

    fun record(url: String?, bootId: String, bundleId: String, sourceDir: String): JSONObject =
        JSONObject(control.toString()).apply {
            put("pid", Process.myPid()); put("boot_id", bootId); put("bundle_id", bundleId)
            put("executable", java.io.File(sourceDir).toURI().toString())
            if (url != null) put("phase", url.substringAfterLast('/'))
        }
}
