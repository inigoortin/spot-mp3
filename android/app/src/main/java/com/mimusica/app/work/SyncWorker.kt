package com.mimusica.app.work

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.mimusica.app.App
import com.mimusica.app.MainActivity
import com.mimusica.app.R
import com.mimusica.app.data.AgentApi
import com.mimusica.app.data.AgentException
import com.mimusica.app.data.Discovery
import com.mimusica.app.data.LocalCache
import com.mimusica.app.data.Order
import com.mimusica.app.data.Outbox
import com.mimusica.app.data.Settings
import java.util.concurrent.TimeUnit

/**
 * Delivers pending jobs to the Mac and follows the sent ones until they finish.
 * - Only runs on Wi-Fi (unmetered network).
 * - If the Mac does not answer, WorkManager retries with growing delays (30 s, 1 min, 2 min...
 *   up to 5 h), also with the app closed or after rebooting the phone.
 * - While any job is still running on the Mac, it reschedules itself in 20 s.
 */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val settings = Settings(applicationContext)
        val outbox = Outbox(applicationContext)
        if (!settings.paired) return Result.success()
        if (outbox.all().none { it.isOpen }) return Result.success()

        if (!Discovery(applicationContext, settings).ensureReachable()) return Result.retry()
        val api = AgentApi(settings)

        try {
            // 1. Deliver the pending ones
            for (order in outbox.all().filter { it.state == Order.PENDING }) {
                // Checked again right before sending (it may have just been cancelled) and marked as attempted: if
                // the response never arrives, cancelling on the phone is no longer enough
                var go = false
                outbox.update(order.clientId) { if (it.state == Order.PENDING) { go = true; it.copy(attempted = true) } else it }
                if (!go) continue
                try {
                    val job = api.submitJob(order.clientId, order.type, order.params)
                    // If it was cancelled while being sent it stays CANCELLING: resolved in step 1b
                    outbox.update(order.clientId) {
                        // Also if another worker marked it cancelled while this one was delivering it: resolved against
                        // the Mac's real job in step 1b
                        if (it.state == Order.CANCELLING || it.state == Order.CANCELLED) it.copy(state = Order.CANCELLING, jobId = job.id)
                        else it.copy(state = Order.SENT, jobId = job.id, message = null)
                    }
                } catch (e: AgentException) {
                    if (e.code == 401) throw e
                    if (e.code !in 400..499) throw e
                    // Job refused by the Mac (invalid data): no point retrying it
                    val msg = e.message ?: "Orden rechazada"
                    val cancelled = outbox.all().firstOrNull { it.clientId == order.clientId }?.state == Order.CANCELLING
                    outbox.update(order.clientId) {
                        if (cancelled) it.copy(state = Order.CANCELLED, message = "Cancelada") else it.copy(state = Order.FAILED, message = msg)
                    }
                    if (!cancelled) notify(order, false, msg)
                }
            }
            // 1b. Cancellations requested on the phone for jobs that may have reached the Mac
            for (order in outbox.all().filter { it.state == Order.CANCELLING }) {
                try {
                    api.cancelJob(order.clientId)
                    outbox.update(order.clientId) { it.copy(state = Order.CANCELLED, message = "Cancelada") }
                } catch (e: AgentException) {
                    when {
                        e.code == 401 -> throw e
                        e.code == 404 && e.message == "no existe" && order.jobId == null ->   // the Mac never received it
                            outbox.update(order.clientId) { it.copy(state = Order.CANCELLED, message = "Cancelada") }
                        e.code == 409 || e.code == 404 -> {
                            // Too late (or an agent that cannot cancel): carry on and always notify
                            val msg = if (e.code == 409) "No se pudo cancelar: ${e.message}" else "No se pudo cancelar: actualiza el agente del Mac"
                            outbox.update(order.clientId) { it.copy(state = Order.SENT, message = msg, cancelFailed = true) }
                            notify(order, false, msg)
                        }
                        else -> throw e   // the Mac does not answer: retry
                    }
                }
            }
            // 2. Follow the sent ones
            var checkedReviews = false
            for (order in outbox.all().filter { it.state == Order.SENT }) {
                val job = try {
                    api.job(order.clientId)
                } catch (e: AgentException) {
                    if (e.code != 404) throw e
                    outbox.update(order.clientId) { it.copy(state = Order.FAILED, message = "El Mac no tiene esta orden") }
                    continue
                }
                if (job.status == "cancelled") {
                    outbox.update(order.clientId) { it.copy(state = Order.CANCELLED, message = "Cancelada") }
                    continue
                }
                if (!job.isActive) {
                    val ok = job.status == "done"
                    val message = describe(job.type, ok, job.result, job.logTail)
                    outbox.update(order.clientId) {
                        // If cancelling was attempted too late, keep that notice
                        it.copy(state = if (ok) Order.DONE else Order.FAILED, message = if (it.cancelFailed) it.message else message)
                    }
                    if (ok) applyToCache(order, api, job.result)
                    if (!(ok && order.isSilent)) notify(order, ok, message)   // edits are already visible in the app
                    // After a job from the phone (e.g. updating the library): new versions to review? Checked now
                    // (once per pass), in case another job fails afterwards
                    if (!order.isSilent && !checkedReviews) {
                        ReviewNotifier.afterOrder(applicationContext, api)
                        checkedReviews = true
                    }
                }
            }
        } catch (e: AgentException) {
            if (e.code == 401) {        // the Mac no longer recognises this phone: pair again
                settings.forget()
                LocalCache(applicationContext).clear()   // what was saved from that Mac is no longer valid
                return Result.failure()
            }
            return Result.retry()
        } catch (e: Exception) {
            return Result.retry()      // Mac off/asleep or Wi-Fi down: WorkManager retries
        }

        if (outbox.all().any { it.isOpen }) schedule(applicationContext, delaySeconds = 20)
        return Result.success()
    }

    /**
     * What was done on the Mac goes into the phone's copy too: otherwise, with the app closed, the job is
     * delivered, the outbox forgets it and offline the old state would show again.
     */
    private fun applyToCache(order: Order, api: AgentApi, result: org.json.JSONObject?) {
        val cache = LocalCache(applicationContext)
        cache.apply(order.type, order.params)
        // Creating, publishing, rebuilding or updating change the list and the songs: save the Mac's state
        // (the list, the new one, the saved details and, if the library changed, status, options and library)
        if (order.type !in setOf(Order.BUILD, "publish", "rebuild_playlists", "refresh")) return
        val asked = System.currentTimeMillis()
        try {
            val status = api.status()
            val playlists = api.playlists()
            cache.putStatus(status.text)
            cache.putPlaylists(playlists.text)
            cache.markSaved()
            cache.prunePlaylists(playlists.value.map { it.name })
            val created = if (order.type == Order.BUILD)
                result?.optString("playlist")?.takeIf { it.isNotEmpty() } ?: order.params.optString("name") else null
            // Each request separately: if one fails (e.g. 404 for one just deleted) the rest go on
            fun save(block: () -> Unit) {
                try { block() } catch (e: AgentException) { if (e.code == 401) throw e }
            }
            for (p in playlists.value) {
                if (p.name == created || cache.hasPlaylist(p.name)) save { cache.putPlaylist(p.name, api.playlist(p.name).text) }
            }
            if (order.type == "refresh") {
                save { cache.putMeta(api.meta().text) }
                save { cache.putSongs("${status.value.songs}|${status.value.lastRefresh}", api.library().text) }
            }
            cache.reapplySince(asked)
        } catch (e: AgentException) {
            if (e.code == 401) throw e   // handled as usual: forget the pairing and the copy
            cache.reapplySince(asked)    // another error: keep what is saved until the next reload
        } catch (e: Exception) {
            cache.reapplySince(asked)
        }
    }

    private fun describe(type: String, ok: Boolean, result: org.json.JSONObject?, log: List<String>): String {
        if (!ok) return result?.optString("error")?.takeIf { it.isNotEmpty() } ?: log.lastOrNull() ?: "Ha fallado"
        val name = result?.optString("playlist").orEmpty()
        val count = result?.optInt("count") ?: 0
        return when (type) {
            "refresh" -> result?.optJSONArray("summary")?.let { s ->
                (0 until s.length()).map { s.getString(it) }
                    .filter { it.startsWith("Descargadas") || it.startsWith("No hay") || it.startsWith("Limpieza") }
                    .takeIf { it.isNotEmpty() }?.joinToString("\n") } ?: "Biblioteca actualizada"
            "build_playlist" -> "'$name' lista con $count canciones"
            "publish" -> "'$name' publicada en Spotify"
            Order.EDIT -> "'$name' actualizada en el Mac"
            "spotify_playlist" -> if (result?.optBoolean("download") == true) "'$name' se descargará; el Mac está actualizando la biblioteca"
                                  else "'$name' ya no se descarga"
            Order.DELETE -> "'$name' eliminada del Mac"
            "rebuild_playlists" -> "Playlists regeneradas"
            Order.REVIEW -> log.lastOrNull() ?: "Hecho"
            else -> "Hecho"
        }
    }

    private fun notify(order: Order, ok: Boolean, message: String) {
        if (ContextCompat.checkSelfPermission(applicationContext, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) return
        val open = PendingIntent.getActivity(
            applicationContext, 0, Intent(applicationContext, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(applicationContext, App.CHANNEL_ORDERS)
            .setSmallIcon(R.drawable.ic_stat_music)
            .setContentTitle(if (ok) "✓ ${order.label}" else "✗ ${order.label}")
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(applicationContext).notify(order.clientId.hashCode(), n)
    }

    companion object {
        private const val WORK = "sync"

        /** Schedules (or reschedules) job delivery/follow-up. */
        fun schedule(context: Context, delaySeconds: Long = 0) {
            val request = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.UNMETERED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .setInitialDelay(delaySeconds, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(WORK, ExistingWorkPolicy.REPLACE, request)
        }
    }
}
