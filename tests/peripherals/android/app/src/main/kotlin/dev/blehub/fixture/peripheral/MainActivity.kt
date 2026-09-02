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
            COMMAND_NOTIFICATION_BURST -> runBurst(
                intent,
                controlId,
                SubscriptionMode.NOTIFICATION,
            )
            COMMAND_INDICATION_BURST -> runBurst(
                intent,
                controlId,
                SubscriptionMode.INDICATION,
            )
            COMMAND_WRITE_BATCH_START -> runWriteBatchStart(intent, controlId)
            COMMAND_WRITE_BATCH_STATUS -> runWriteBatchStatus(intent, controlId)
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
        intent?.removeExtra(EXTRA_BURST_ID)
        intent?.removeExtra(EXTRA_COUNT)
        intent?.removeExtra(EXTRA_PAYLOAD_BYTES)
        intent?.removeExtra(EXTRA_CONTROL_ID)
    }

    private fun requireValue(intent: Intent?): String =
        requireNotNull(intent?.getStringExtra(EXTRA_VALUE)) { "command requires a hex value" }

    private fun runBurst(
        intent: Intent?,
        controlId: String?,
        mode: SubscriptionMode,
    ) {
        val operation = mode.burstCommand()
        var count: Int? = null
        try {
            count = requireBurstCount(intent, operation)
            val burstIdText = requireNotNull(intent?.getStringExtra(EXTRA_BURST_ID)) {
                "$operation requires burst_id"
            }
            require(BURST_ID_PATTERN.matches(burstIdText)) {
                "burst_id must be exactly 16 lowercase hexadecimal characters"
            }
            val payloadBytes = intent?.getIntExtra(EXTRA_PAYLOAD_BYTES, DEFAULT_BURST_PAYLOAD_BYTES)
                ?: DEFAULT_BURST_PAYLOAD_BYTES
            val onComplete = { completion: NotificationBurstCompletion ->
                completeNotificationBurst(controlId, completion)
            }
            when (mode) {
                SubscriptionMode.NOTIFICATION -> server.sendNotificationBurst(
                    burstIdText.decodeHex(),
                    count,
                    payloadBytes,
                    onComplete,
                )
                SubscriptionMode.INDICATION -> server.sendIndicationBurst(
                    burstIdText.decodeHex(),
                    count,
                    payloadBytes,
                    onComplete,
                )
                SubscriptionMode.DISABLED -> error("disabled subscriptions cannot send bursts")
            }
        } catch (error: Exception) {
            val message = error.message ?: error::class.java.simpleName
            appendLog("ERROR: $message")
            acknowledge(
                controlId,
                false,
                message,
                "operation=$operation count=${count ?: "invalid"} " +
                    "mode=${mode.name.lowercase()}",
            )
        }
    }

    private fun requireBurstCount(intent: Intent?, operation: String): Int {
        require(intent?.hasExtra(EXTRA_COUNT) == true) {
            "$operation requires count"
        }
        val count = intent.getIntExtra(EXTRA_COUNT, 0)
        require(count in MIN_BURST_COUNT..MAX_BURST_COUNT) {
            "$operation count must be between $MIN_BURST_COUNT and $MAX_BURST_COUNT"
        }
        return count
    }

    private fun runWriteBatchStart(intent: Intent?, controlId: String?) {
        var count: Int? = null
        try {
            count = requireBurstCount(intent, COMMAND_WRITE_BATCH_START)
            val batchId = requireBatchId(intent)
            server.startWriteBatch(batchId, count)
            acknowledge(
                controlId,
                true,
                null,
                "operation=$COMMAND_WRITE_BATCH_START batch_id=${batchId.toHex()} count=$count",
            )
        } catch (error: Exception) {
            val message = error.message ?: error::class.java.simpleName
            appendLog("ERROR: $message")
            acknowledge(
                controlId,
                false,
                message,
                "operation=$COMMAND_WRITE_BATCH_START count=${count ?: "invalid"}",
            )
        }
    }

    private fun runWriteBatchStatus(intent: Intent?, controlId: String?) {
        try {
            val snapshot = server.writeBatchSnapshot(requireBatchId(intent))
            val details = buildString {
                append("operation=$COMMAND_WRITE_BATCH_STATUS")
                append(" batch_id=${snapshot.batchId.toHex()}")
                append(" expected_count=${snapshot.expectedCount}")
                append(" received_count=${snapshot.receivedCount}")
                append(" unique_count=${snapshot.uniqueCount}")
                append(" missing_count=${snapshot.missingCount}")
                append(" duplicate_count=${snapshot.duplicateCount}")
                append(" out_of_order_count=${snapshot.outOfOrderCount}")
                append(" out_of_range_count=${snapshot.outOfRangeCount}")
                append(" foreign_batch_count=${snapshot.foreignBatchCount}")
                append(" malformed_count=${snapshot.malformedCount}")
                append(" callback_span_ns=${snapshot.callbackSpanNanos}")
            }
            acknowledge(controlId, true, null, details)
        } catch (error: Exception) {
            val message = error.message ?: error::class.java.simpleName
            appendLog("ERROR: $message")
            acknowledge(controlId, false, message, "operation=$COMMAND_WRITE_BATCH_STATUS")
        }
    }

    private fun requireBatchId(intent: Intent?): ByteArray {
        val batchIdText = requireNotNull(intent?.getStringExtra(EXTRA_BURST_ID)) {
            "write batch command requires batch_id"
        }
        require(BURST_ID_PATTERN.matches(batchIdText)) {
            "batch_id must be exactly 16 lowercase hexadecimal characters"
        }
        return batchIdText.decodeHex()
    }

    private fun completeNotificationBurst(
        controlId: String?,
        completion: NotificationBurstCompletion,
    ) {
        val details =
            "operation=${completion.mode.burstCommand()} count=${completion.count} " +
                "payload_bytes=${completion.payloadBytes} " +
                "local_elapsed_ns=${completion.localElapsedNanos} " +
                "mode=${completion.mode.name.lowercase()}"
        val error = completion.error
        if (error == null) {
            acknowledge(controlId, true, null, details)
        } else {
            acknowledge(controlId, false, error, details)
            runOnUiThread { appendLog("ERROR: $error") }
        }
    }

    private fun SubscriptionMode.burstCommand(): String = when (this) {
        SubscriptionMode.NOTIFICATION -> COMMAND_NOTIFICATION_BURST
        SubscriptionMode.INDICATION -> COMMAND_INDICATION_BURST
        SubscriptionMode.DISABLED -> error("disabled subscriptions do not have a burst command")
    }

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

    private fun acknowledge(
        controlId: String?,
        succeeded: Boolean,
        message: String?,
        details: String? = null,
    ) {
        if (controlId == null) return
        val detailSuffix = details?.let { " $it" }.orEmpty()
        if (succeeded) {
            Log.i(LOG_TAG, "control id=$controlId result=ok$detailSuffix")
        } else {
            Log.e(LOG_TAG, "control id=$controlId result=error$detailSuffix message=$message")
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
        private const val EXTRA_BURST_ID = "burst_id"
        private const val EXTRA_COUNT = "count"
        private const val EXTRA_PAYLOAD_BYTES = "payload_bytes"
        private const val EXTRA_CONTROL_ID = "control_id"
        private const val COMMAND_START = "start"
        private const val COMMAND_STOP = "stop"
        private const val COMMAND_NOTIFY = "notify"
        private const val COMMAND_INDICATE = "indicate"
        private const val COMMAND_NOTIFICATION_BURST = "notification-burst"
        private const val COMMAND_INDICATION_BURST = "indication-burst"
        private const val COMMAND_WRITE_BATCH_START = "write-batch-start"
        private const val COMMAND_WRITE_BATCH_STATUS = "write-batch-status"
        private const val COMMAND_DISCONNECT = "disconnect"
        private const val COMMAND_STATUS = "status"
        private const val COMMAND_READY = "ready"
        private const val MIN_BURST_COUNT = 1
        private const val MAX_BURST_COUNT = 10_000
        private const val DEFAULT_BURST_PAYLOAD_BYTES = 20
        private val BURST_ID_PATTERN = Regex("[0-9a-f]{16}")
        private val REQUIRED_PERMISSIONS = arrayOf(
            Manifest.permission.BLUETOOTH_ADVERTISE,
            Manifest.permission.BLUETOOTH_CONNECT,
        )
    }
}
