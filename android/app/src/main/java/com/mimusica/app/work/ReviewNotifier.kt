package com.mimusica.app.work

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.mimusica.app.App
import com.mimusica.app.MainActivity
import com.mimusica.app.R
import com.mimusica.app.data.AgentApi

/**
 * Notification for versions to review. There is no periodic check: it only looks when a job started
 * from the phone finishes (SyncWorker is already following it) and notifies if there is a new one
 * not seen yet (in the app or in an earlier notification).
 */
object ReviewNotifier {
    private const val PREFS = "reviews"
    private const val NOTIFICATION_ID = 4711

    /** After a finished job: a single request to the Mac; errors do not affect the job. */
    fun afterOrder(context: Context, api: AgentApi) {
        val pending = runCatching { api.reviews() }.getOrNull() ?: return
        val fresh = pending.filter { it.trackId !in seen(context) }
        // Only counted as seen if the notification could be shown (with notifications denied, it will notify later)
        if (fresh.isEmpty() || notify(context, pending.size, fresh.first().song)) markSeen(context, pending.map { it.trackId })
    }

    private fun notify(ctx: Context, total: Int, example: String): Boolean {
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return false
        val open = PendingIntent.getActivity(
            ctx, 1, Intent(ctx, MainActivity::class.java).putExtra(MainActivity.EXTRA_OPEN, "reviews")
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val text = if (total == 1) "$example: no es la versión de Spotify. ¿La apruebas o la quitas?"
                   else "$total canciones no tienen la versión de Spotify (p. ej. $example). Escúchalas y decide."
        val n = NotificationCompat.Builder(ctx, App.CHANNEL_REVIEWS)
            .setSmallIcon(R.drawable.ic_stat_music)
            .setContentTitle(if (total == 1) "1 versión para revisar" else "$total versiones para revisar")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(ctx).notify(NOTIFICATION_ID, n)
        return true
    }

    private fun seen(context: Context): Set<String> =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getStringSet("seen", emptySet()).orEmpty()

    /** The ones already seen (in the app or notified): never notified again. */
    fun markSeen(context: Context, ids: List<String>) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putStringSet("seen", ids.toSet()).apply()
    }
}
