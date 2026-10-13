package com.mimusica.app.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.time.LocalDateTime
import java.util.UUID

/**
 * Outbox: every job is saved on the phone first and SyncWorker delivers it when the Mac is
 * reachable. The clientId makes sure that, if retried, the Mac does not run it twice.
 */
data class Order(
    val clientId: String,
    val type: String,
    val params: JSONObject,
    val label: String,
    val created: String,
    val state: String = PENDING,   // pending -> sent -> done | failed; cancelling -> cancelled | sent
    val jobId: String? = null,
    val message: String? = null,
    /**
     * Already tried to send it to the Mac (even without a response): it may have arrived, cancelling
     * here is not enough.
     */
    val attempted: Boolean = false,
    /** Cancellation was requested but the Mac had already started or finished it: shown even if silent. */
    val cancelFailed: Boolean = false,
) {
    val isOpen get() = state == PENDING || state == SENT || state == CANCELLING
    /** Phone edits: applied silently and visible in the playlist itself. */
    val isPlaylistEdit get() = type == EDIT || type == DELETE
    /** Applied silently (no notification or card when they succeed): edits and reviews. */
    val isSilent get() = isPlaylistEdit || type == REVIEW

    fun toJson(): JSONObject = JSONObject()
        .put("client_id", clientId).put("type", type).put("params", params).put("label", label)
        .put("created", created).put("state", state).put("job_id", jobId).put("message", message)
        .put("attempted", attempted).put("cancel_failed", cancelFailed)

    companion object {
        const val PENDING = "pending"
        const val SENT = "sent"
        const val DONE = "done"
        const val FAILED = "failed"
        const val CANCELLED = "cancelled"
        const val CANCELLING = "cancelling"   // cancellation requested from the Mac, no response yet
        const val EDIT = "edit_playlist"
        const val DELETE = "delete_playlist"
        const val REVIEW = "review_version"
        const val BUILD = "build_playlist"

        fun from(o: JSONObject) = Order(
            clientId = o.getString("client_id"), type = o.getString("type"),
            params = o.optJSONObject("params") ?: JSONObject(), label = o.optString("label"),
            created = o.optString("created"), state = o.optString("state", PENDING),
            jobId = o.optStringOrNull("job_id"), message = o.optStringOrNull("message"),
            attempted = o.optBoolean("attempted"), cancelFailed = o.optBoolean("cancel_failed"),
        )
    }
}

/**
 * The screen (AppViewModel) and SyncWorker each create their own Outbox: all instances share one
 * lock so a job added while the worker updates another is not lost (read-modify-write on the same
 * preferences).
 */
class Outbox(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("outbox", Context.MODE_PRIVATE)

    private companion object { val LOCK = Any() }

    fun all(): List<Order> = synchronized(LOCK) {
        JSONArray(prefs.getString("orders", "[]")).objects().map { Order.from(it) }
    }

    private fun saveAll(orders: List<Order>) = synchronized(LOCK) {
        // Keep every open job and the last 40 finished ones (playlist edits already applied need not be
        // remembered)
        val closed = orders.filter { !it.isOpen && !(it.isPlaylistEdit && it.state == Order.DONE && !it.cancelFailed) }.takeLast(40)
        val keep = orders.filter { it.isOpen || it in closed }
        prefs.edit().putString("orders", JSONArray(keep.map { it.toJson() }).toString()).commit()
    }

    fun add(type: String, params: JSONObject, label: String): Order = synchronized(LOCK) {
        val order = Order(UUID.randomUUID().toString(), type, params, label,
                          LocalDateTime.now().withNano(0).toString())
        saveAll(all() + order)
        order
    }

    fun update(clientId: String, transform: (Order) -> Order) = synchronized(LOCK) {
        saveAll(all().map { if (it.clientId == clientId) transform(it) else it })
    }

    /**
     * Cancel: if it was never sent, it is cancelled right here (returns CANCELLED); if it may have
     * reached the Mac (sent, or tried without a response) it becomes CANCELLING and SyncWorker asks the
     * Mac (returns CANCELLING). null if it can no longer be cancelled (finished).
     */
    fun requestCancel(clientId: String): String? = synchronized(LOCK) {
        val orders = all()
        val o = orders.firstOrNull { it.clientId == clientId } ?: return null
        val next = when {
            o.state == Order.PENDING && !o.attempted -> Order.CANCELLED
            o.state == Order.PENDING || o.state == Order.SENT -> Order.CANCELLING
            o.state == Order.CANCELLING -> return Order.CANCELLING
            else -> return null
        }
        saveAll(orders.map {
            if (it.clientId == clientId) it.copy(state = next, message = if (next == Order.CANCELLED) "Cancelada" else "Cancelando…") else it
        })
        next
    }

    fun clearFinished() = synchronized(LOCK) { saveAll(all().filter { it.isOpen }) }
}
