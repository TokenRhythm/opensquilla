package ai.opensquilla.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Foreground service that keeps the local AI gateway alive while the app is
 * in the background. Without it, aggressive OEM killers (EMUI/MIUI/ColorOS)
 * freeze the process the moment the user switches away, and the WebView loses
 * its 127.0.0.1:18790 backend.
 *
 * Uses foregroundServiceType="specialUse" (no time limit, unlike dataSync on
 * Android 14+) because the gateway must run for as long as the user wants it.
 */
class GatewayService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFICATION_ID, buildNotification())
        // Python is always started by MainActivity's background thread (never on
        // the UI thread). The service only makes sure the gateway is serving;
        // it must NOT call Python.start() itself — concurrent init would race.
        ensureGatewayServing()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ensureGatewayServing()
        return START_STICKY
    }

    /**
     * Single-instance guard: MainActivity is the *only* caller of
     * opensquilla_android.serve() — the service must never start a second
     * gateway (a concurrent serve would hit the pid-lock and spam
     * "already_running"). The service only keeps the process foregrounded;
     * if the port is down we simply schedule a liveness probe.
     */
    private fun ensureGatewayServing() {
        if (!isPortOpen("127.0.0.1", 18790)) {
            // MainActivity boots the gateway on its own thread. Nothing to do
            // here; just re-check later via onStartCommand/START_STICKY.
            return
        }
    }

    private fun isPortOpen(host: String, port: Int): Boolean = try {
        Socket().use { s -> s.connect(InetSocketAddress(host, port), 300) }
        true
    } catch (e: Exception) {
        false
    }

    private fun buildNotification(): Notification {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "本地 AI 网关",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "保持本地 AI 网关在后台持续运行"
                setShowBadge(false)
            }
            manager.createNotificationChannel(channel)
        }
        val contentIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(android.R.drawable.ic_menu_compass)
            .setContentTitle("OpenSquilla 运行中")
            .setContentText("本地 AI 网关正在 127.0.0.1:18790 提供服务")
            .setContentIntent(contentIntent)
            .setOngoing(true)
            .setShowWhen(false)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = "gateway"
        private const val NOTIFICATION_ID = 1001

        /** Start (or restart) the keeper service. Safe to call repeatedly. */
        fun start(context: Context) {
            val intent = Intent(context, GatewayService::class.java)
            context.startForegroundService(intent)
        }
    }
}
