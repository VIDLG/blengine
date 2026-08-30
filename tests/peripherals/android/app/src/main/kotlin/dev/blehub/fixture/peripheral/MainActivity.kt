package dev.blehub.fixture.peripheral

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.text.InputType
import android.util.Log
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class MainActivity : Activity() {
    private lateinit var server: BlePeripheralServer
    private lateinit var statusView: TextView
    private lateinit var logView: TextView
    private lateinit var valueInput: EditText
    private var awaitingPermission = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val profile = FixtureProfile.load(this)
        buildUi(profile)
        server = BlePeripheralServer(this, profile) { snapshot ->
            runOnUiThread { render(snapshot) }
        }
        if (hasPermissions()) {
            executeIntent(intent, startWhenEmpty = true)
        } else {
            awaitingPermission = true
            requestPermissions(REQUIRED_PERMISSIONS, PERMISSION_REQUEST)
            appendLog("requesting Nearby devices permission")
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        executeIntent(intent, startWhenEmpty = false)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != PERMISSION_REQUEST) return
        awaitingPermission = false
        if (grantResults.isNotEmpty() && grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
            executeIntent(intent, startWhenEmpty = true)
        } else {
            val message = "Nearby devices permission denied"
            appendLog(message)
            acknowledge(intent.getStringExtra(EXTRA_CONTROL_ID), false, message)
            clearControlExtras(intent)
        }
    }

    override fun onDestroy() {
        if (::server.isInitialized) server.close()
        super.onDestroy()
    }

    private fun buildUi(profile: FixtureProfile) {
        val density = resources.displayMetrics.density
        val padding = (16 * density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }
        root.addView(TextView(this).apply {
            text = getString(
                R.string.fixture_title,
                profile.profileId,
                profile.advertising.serviceUuids.joinToString(),
            )
            textSize = 18f
        })
        statusView = TextView(this).apply {
            text = getString(R.string.status_initializing)
            setPadding(0, padding, 0, padding)
        }
        root.addView(statusView)

        valueInput = EditText(this).apply {
            hint = getString(R.string.hex_payload_hint)
            setText(getString(R.string.default_hex_payload))
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS
        }
        root.addView(valueInput, matchWidth())
        root.addView(buttonRow(getString(R.string.start) to {
            runAction(server::start)
        }, getString(R.string.stop) to {
            runAction(server::stop)
        }))
        root.addView(buttonRow(getString(R.string.notify) to {
            runAction({ server.sendNotification(valueInput.text.toString().decodeHex()) })
        }, getString(R.string.indicate) to {
            runAction({ server.sendIndication(valueInput.text.toString().decodeHex()) })
        }))
        root.addView(buttonRow(getString(R.string.disconnect) to {
            runAction(server::disconnectAll)
        }, getString(R.string.status) to {
            runAction(server::reportStatus)
        }))

        logView = TextView(this).apply {
            setTextIsSelectable(true)
            typeface = android.graphics.Typeface.MONOSPACE
        }
        val scroll = ScrollView(this).apply { addView(logView, matchWidth()) }
        root.addView(scroll, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            0,
            1f,
        ))
        setContentView(root)
    }

    private fun buttonRow(
        first: Pair<String, () -> Unit>,
        second: Pair<String, () -> Unit>,
    ): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        addView(Button(this@MainActivity).apply {
            text = first.first
            setOnClickListener { first.second() }
        }, weighted())
        addView(Button(this@MainActivity).apply {
            text = second.first
            setOnClickListener { second.second() }
        }, weighted())
    }

    private fun render(snapshot: PeripheralSnapshot) {
        statusView.text = buildString {
            append("Status: ${snapshot.phase}\n")
            append("Connected: ${snapshot.connectedDevices.ifEmpty { listOf("none") }.joinToString()}\n")
            append("Subscriptions: ${snapshot.subscriptions.ifEmpty { mapOf("none" to SubscriptionMode.DISABLED) }}\n")
            append("Update value: ${snapshot.updateValue.toHex()}")
        }
        appendLog(snapshot.lastEvent)
    }

    private fun appendLog(message: String) {
        if (::logView.isInitialized) {
            logView.append("$message\n")
        }
    }

    private fun executeIntent(intent: Intent?, startWhenEmpty: Boolean) {
        val controlId = intent?.getStringExtra(EXTRA_CONTROL_ID)
        if (!hasPermissions()) {
            val message = "command ignored until permission is granted"
            if (!awaitingPermission) appendLog(message)
            acknowledge(controlId, false, message)
            clearControlExtras(intent)
            return
        }
        val command = intent?.getStringExtra(EXTRA_COMMAND)
        if (command == null && !startWhenEmpty) return
        when (command ?: COMMAND_START) {
            COMMAND_START -> runAction(server::start, controlId)
            COMMAND_STOP -> runAction(server::stop, controlId)
            COMMAND_NOTIFY -> runAction({
                server.sendNotification(requireValue(intent).decodeHex())
            }, controlId)
            COMMAND_INDICATE -> runAction({
                server.sendIndication(requireValue(intent).decodeHex())
            }, controlId)
            COMMAND_DISCONNECT -> runAction(server::disconnectAll, controlId)
            COMMAND_STATUS -> runAction(server::reportStatus, controlId)
            COMMAND_READY -> runAction(server::requireAdvertising, controlId)
            else -> {
                val message = "unknown fixture command: $command"
                appendLog(message)
                acknowledge(controlId, false, message)
            }
        }
        clearControlExtras(intent)
    }

    private fun clearControlExtras(intent: Intent?) {
        intent?.removeExtra(EXTRA_COMMAND)
        intent?.removeExtra(EXTRA_VALUE)
        intent?.removeExtra(EXTRA_CONTROL_ID)
    }

    private fun requireValue(intent: Intent?): String =
        requireNotNull(intent?.getStringExtra(EXTRA_VALUE)) { "command requires a hex value" }

    private fun runAction(action: () -> Unit, controlId: String? = null) {
        try {
            action()
            acknowledge(controlId, true, null)
        } catch (error: Exception) {
            val message = error.message ?: error::class.java.simpleName
            appendLog("ERROR: $message")
            acknowledge(controlId, false, message)
        }
    }

    private fun acknowledge(controlId: String?, succeeded: Boolean, message: String?) {
        if (controlId == null) return
        if (succeeded) {
            Log.i(LOG_TAG, "control id=$controlId result=ok")
        } else {
            Log.e(LOG_TAG, "control id=$controlId result=error message=$message")
        }
    }

    private fun hasPermissions(): Boolean = REQUIRED_PERMISSIONS.all {
        checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED
    }

    private fun matchWidth() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
    )

    private fun weighted() = LinearLayout.LayoutParams(
        0,
        ViewGroup.LayoutParams.WRAP_CONTENT,
        1f,
    )

    companion object {
        private const val LOG_TAG = "BleHubPeripheral"
        private const val PERMISSION_REQUEST = 1
        private const val EXTRA_COMMAND = "fixture_command"
        private const val EXTRA_VALUE = "value"
        private const val EXTRA_CONTROL_ID = "control_id"
        private const val COMMAND_START = "start"
        private const val COMMAND_STOP = "stop"
        private const val COMMAND_NOTIFY = "notify"
        private const val COMMAND_INDICATE = "indicate"
        private const val COMMAND_DISCONNECT = "disconnect"
        private const val COMMAND_STATUS = "status"
        private const val COMMAND_READY = "ready"
        private val REQUIRED_PERMISSIONS = arrayOf(
            Manifest.permission.BLUETOOTH_ADVERTISE,
            Manifest.permission.BLUETOOTH_CONNECT,
        )
    }
}
