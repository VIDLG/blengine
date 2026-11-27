package com.blehubblenghost

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder

/**
 * Keeps this process alive while the App is in the background.
 *
 * Why this exists: a managed scene keeps a CoreBluetooth/BLE session across a
 * background/foreground cycle, and Android is free to kill a backgrounded process. A
 * foreground service is the platform's own answer to that, and it costs the user a
 * persistent notification — which is why it is scoped as narrowly as the claim: it starts
 * when the Activity stops and stops when it starts again, so the notification exists only
 * while the user is away from the App.
 *
 * It is deliberately empty. It owns no BLE state and must never be a second owner of the
 * session: the backend owns connections, and this service only denies the OS the chance to
 * reclaim the process underneath it.
 */
class BleHubKeepAliveService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, notification())
        // Not sticky: a restarted process has no session to keep alive, and pretending
        // otherwise would be a second owner.
        return START_NOT_STICKY
    }

    private fun notification(): Notification {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Connection keep-alive",
            // Low importance: no sound, no heads-up. The notification states the fact and
            // gets out of the way.
            NotificationManager.IMPORTANCE_LOW,
        ).apply { description = "Keeps an active BLE session alive while the App is away" }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("Bleng host is keeping the link")
            .setContentText("A BLE session remains active while the App is in the background")
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "blehub-keep-alive"
        private const val NOTIFICATION_ID = 0x9E

        /** Start from the Activity; the service type is declared in the manifest. */
        fun start(context: Context) {
            val intent = Intent(context, BleHubKeepAliveService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /** Called when the App returns to the foreground: the notification must not linger. */
        fun stop(context: Context) {
            context.stopService(Intent(context, BleHubKeepAliveService::class.java))
        }
    }
}
