package org.hophacks.inkcaretaker

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

object Notifier {
    const val FOREGROUND_ID = 1
    const val EMERGENCY_ID = 2
    const val CHANNEL_LISTEN = "listen"
    const val CHANNEL_EVENTS = "events"
    const val CHANNEL_EMERGENCY = "emergency"

    fun ensureChannels(ctx: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = ctx.getSystemService(NotificationManager::class.java) ?: return
        val listen = NotificationChannel(
            CHANNEL_LISTEN, "Listening", NotificationManager.IMPORTANCE_LOW,
        ).apply { setShowBadge(false) }
        val events = NotificationChannel(
            CHANNEL_EVENTS, "Drawings", NotificationManager.IMPORTANCE_DEFAULT,
        )
        val emergency = NotificationChannel(
            CHANNEL_EMERGENCY, "Emergency help", NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = "Patient asked for help — overrides other alerts"
            enableVibration(true)
            vibrationPattern = longArrayOf(0, 400, 120, 400, 120, 600)
            enableLights(true)
            lightColor = Color.RED
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            setSound(
                RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
            setBypassDnd(true)
        }
        manager.createNotificationChannel(listen)
        manager.createNotificationChannel(events)
        manager.createNotificationChannel(emergency)
    }

    fun listening(ctx: Context, text: String): Notification {
        val open = PendingIntent.getActivity(
            ctx, 0,
            Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(ctx, CHANNEL_LISTEN)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(ctx.getString(R.string.app_name))
            .setContentText(text)
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(open)
            .build()
    }

    fun updateListening(ctx: Context, text: String) {
        try {
            NotificationManagerCompat.from(ctx).notify(FOREGROUND_ID, listening(ctx, text))
        } catch (_: SecurityException) {
        }
    }

    fun event(ctx: Context, event: CaretakerEvent) {
        val open = PendingIntent.getActivity(
            ctx, event.id.hashCode(),
            Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(ctx, CHANNEL_EVENTS)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(event.badge() + " · " + Prefs.patientName(ctx))
            .setContentText(event.headline())
            .setStyle(NotificationCompat.BigTextStyle().bigText(event.headline()))
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(open)
            .build()
        try {
            NotificationManagerCompat.from(ctx).notify(event.id.hashCode(), notification)
        } catch (_: SecurityException) {
        }
    }

    fun emergency(ctx: Context, event: CaretakerEvent) {
        val screen = Intent(ctx, EmergencyActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        event.extras(screen)
        screen.putExtra("patient", Prefs.patientName(ctx))
        val full = PendingIntent.getActivity(
            ctx, 99, screen,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(ctx, CHANNEL_EMERGENCY)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle("HELP · ${Prefs.patientName(ctx)}")
            .setContentText(event.headline())
            .setStyle(NotificationCompat.BigTextStyle().bigText(event.headline()))
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setOngoing(true)
            .setAutoCancel(false)
            .setColor(ctx.getColor(R.color.emergency))
            .setFullScreenIntent(full, true)
            .setContentIntent(full)
            .setVibrate(longArrayOf(0, 400, 120, 400, 120, 600))
            .build()
        try {
            NotificationManagerCompat.from(ctx).notify(EMERGENCY_ID, notification)
        } catch (_: SecurityException) {
        }
    }

    fun clearEmergency(ctx: Context) {
        NotificationManagerCompat.from(ctx).cancel(EMERGENCY_ID)
    }
}
