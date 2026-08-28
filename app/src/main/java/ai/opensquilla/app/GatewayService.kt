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
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
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
        // Python may already be started by the activity; startGuard is idempotent.
        if (!Python.isStarted()) {
            try {
                Python.start(AndroidPlatform(this))
            } catch (e: Exception) {
                // Already started elsewhere (races on quick backgrounding).
            }
        }
        ensureGatewayServing()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        ensureGatewayServing()
        return START_STICKY
    }

    /**
     * Ask opensquilla_android.serve() to run the FastAPI gateway, but only if
     * the port is not already listening. Calling serve() twice would rebind
     * the port; the health probe keeps this idempotent.
     */
    private fun ensureGatewayServing() {
        val py = try {
            Python.getInstance()
        } catch (e: Exception) {
            return
        }
        try {
            if (isPortOpen("127.0.0.1", 18790)) {
                // Already serving; nothing to do.
                return
            }
            py.getModule("opensquilla_android")
                .callAttr("serve", filesDir.absolutePath)
        } catch (e: Exception) {
            // serve() may be mid-startup; the gateway thread owns the port.
            // Ignore and keep the service alive.
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
