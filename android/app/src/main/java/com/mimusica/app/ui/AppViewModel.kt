package com.mimusica.app.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.mimusica.app.data.AgentApi
import com.mimusica.app.data.AgentException
import com.mimusica.app.data.AgentStatus
import com.mimusica.app.data.Discovery
import com.mimusica.app.data.LocalCache
import com.mimusica.app.data.Meta
import com.mimusica.app.data.Order
import com.mimusica.app.data.Outbox
import com.mimusica.app.data.PlaylistDetail
import com.mimusica.app.data.PlaylistSummary
import com.mimusica.app.data.Settings
import com.mimusica.app.data.Song
import com.mimusica.app.data.objects
import com.mimusica.app.data.strings
import com.mimusica.app.work.ReviewNotifier
import com.mimusica.app.work.SyncWorker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject

enum class Connection { SEARCHING, NOT_FOUND, NEEDS_PAIRING, ONLINE }

data class UiState(
    val connection: Connection = Connection.SEARCHING,
    val macAddress: String? = null,
    val status: AgentStatus? = null,
    val meta: Meta? = null,
    val playlists: List<PlaylistSummary> = emptyList(),
    val detail: PlaylistDetail? = null,
    val detailLoading: Boolean = false,
    val orders: List<Order> = emptyList(),
    val liveLog: Map<String, List<String>> = emptyMap(),   // job id on the Mac -> lines
    val pairingId: String? = null,
    val reviews: List<com.mimusica.app.data.Review> = emptyList(),
    val message: String? = null,
    val syncing: Boolean = false,
    val spotifyPlaylists: List<com.mimusica.app.data.SpotifyPlaylist>? = null,   // null = not loaded
    val spotifyLoading: Boolean = false,
    val spotifyError: String? = null,
    /** When the data shown offline was saved (see LocalCache). */
    val savedAt: String? = null,
)

class AppViewModel(app: Application) : AndroidViewModel(app) {

    private val settings = Settings(app)
    private val outbox = Outbox(app)
    private val discovery = Discovery(app, settings)
    private val cache = LocalCache(app)
    val api = AgentApi(settings)

    // Start with what was saved last time: without the Mac it can be viewed and edited all the same
    private val _state = MutableStateFlow(UiState(orders = outbox.all().reversed(), status = cache.status(),
                                                  meta = cache.meta(), savedAt = cache.savedAt()))
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var socket: WebSocket? = null
    private var socketConnected = false

    // What the Mac says; the screen shows it with pending edits already applied
    private var serverPlaylists: List<PlaylistSummary> = cache.playlists().orEmpty()
    private var serverDetail: PlaylistDetail? = null
    private var detailTarget: String? = null        // playlist currently open (the last one requested)
    private var loadSeq = 0                         // only the response to the latest request counts
    private var listSeq = 0
    private var lastOpenEdits = outbox.all().filter { it.isPlaylistEdit && it.isOpen }
    // Edits already applied on the Mac that keep showing until their new data arrives (otherwise what
    // was removed would reappear for a moment)
    private var appliedOverlay: List<Order> = emptyList()
    // Playlists created on the phone waiting for the Mac: the list is reloaded when they finish
    private var lastOpenBuilds = outbox.all().filter { it.type == Order.BUILD && it.isOpen }.map { it.clientId }.toSet()
    // Form options and library: requested once per session and when the library changes
    private var libraryChecked = false
    // Bumped when the saved data is erased (pair or forget): an older Mac response is no longer saved
    private var generation = 0

    init {
        showWithPending()
        connect()
        // Outbox and WebSocket reconnection while the app is open
        viewModelScope.launch {
            while (isActive) {
                _state.update { it.copy(orders = outbox.all().reversed()) }
                // If the Mac applied edits (e.g. when syncing after being offline), reload its data: the screen
                // must no longer depend on the pending ones
                val all = outbox.all()
                val open = all.filter { it.isPlaylistEdit && it.isOpen }
                val failedIds = all.filter { it.state == Order.FAILED || it.state == Order.CANCELLED }.map { it.clientId }.toSet()
                val openIds = open.map { it.clientId }.toSet()
                val applied = lastOpenEdits.filter { it.clientId !in openIds && it.clientId !in failedIds }
                if (lastOpenEdits.any { it.clientId !in openIds }) {
                    appliedOverlay = appliedOverlay + applied
                    refreshAll()
                    detailTarget?.let { loadPlaylist(it) }
                }
                lastOpenEdits = open
                val openBuilds = all.filter { it.type == Order.BUILD && it.isOpen }.map { it.clientId }.toSet()
                if (lastOpenBuilds.any { it !in openBuilds }) refreshAll()
                lastOpenBuilds = openBuilds
                showWithPending()
                if (_state.value.connection == Connection.ONLINE && !socketConnected) openSocket()
                delay(3000)
            }
        }
    }

    fun say(msg: String?) = _state.update { it.copy(message = msg) }

    private suspend fun <T> io(block: () -> T): T = withContext(Dispatchers.IO) { block() }

    /** Finds the Mac and, if paired, loads everything. */
    fun connect() = viewModelScope.launch {
        _state.update { it.copy(connection = Connection.SEARCHING) }
        val reachable = discovery.ensureReachable()
        when {
            !reachable -> _state.update { it.copy(connection = Connection.NOT_FOUND, macAddress = settings.host) }
            !settings.paired -> _state.update { it.copy(connection = Connection.NEEDS_PAIRING, macAddress = settings.host) }
            else -> {
                _state.update { it.copy(connection = Connection.ONLINE, macAddress = settings.host) }
                refreshAll()
                SyncWorker.schedule(getApplication())
            }
        }
    }

    fun setManualAddress(ip: String) {
        settings.host = ip.trim().ifEmpty { null }
        connect()
    }

    /** `userAction`: requested by the user (e.g. pairing), so always report if the Mac does not answer. */
    private suspend fun guard(userAction: Boolean = false, block: suspend () -> Unit) {
        val wasOnline = _state.value.connection == Connection.ONLINE
        try {
            block()
        } catch (e: AgentException) {
            if (e.code == 0) return offline(wasOnline || userAction)   // no Mac address
            if (e.code == 401) lostPairing()
            say(e.message)
        } catch (e: Exception) {
            offline(wasOnline || userAction)
        }
    }

    /**
     * The Mac does not answer: carry on with what is saved; on reloads only report losing the connection.
     */
    private fun offline(tell: Boolean) {
        _state.update { it.copy(connection = Connection.NOT_FOUND, savedAt = cache.savedAt()) }
        if (tell) say("No se puede contactar con el Mac" + if (_state.value.savedAt != null) ": se muestran los datos guardados" else "")
    }

    /** 401: the Mac no longer recognises this phone; what was saved from that Mac is no longer valid. */
    private fun lostPairing() {
        settings.forget()
        clearSaved()
        _state.update { it.copy(connection = Connection.NEEDS_PAIRING) }
    }

    /**
     * Sync button: delivers pending jobs, reloads everything from the Mac and reports the result (it
     * used to reload silently and seemed to do nothing).
     */
    private var lastRefreshOk = false

    fun syncNow() = viewModelScope.launch {
        if (_state.value.syncing) return@launch
        lastRefreshOk = false
        _state.update { it.copy(syncing = true) }
        if (_state.value.connection != Connection.ONLINE) {
            connect().join()
        }
        if (_state.value.connection == Connection.ONLINE) {
            SyncWorker.schedule(getApplication())          // jobs saved on the phone
            refreshAll().join()
            detailTarget?.let { loadPlaylist(it).join() }
            val pending = outbox.all().count { it.isOpen }
            val s = _state.value.status
            // If the reload failed, its error was already shown: do not cover it with "Sincronizado"
            if (lastRefreshOk && _state.value.connection == Connection.ONLINE) say(
                "Sincronizado: ${s?.songs ?: 0} canciones, ${s?.playlists ?: 0} playlists" +
                    if (pending > 0) " · $pending ${if (pending == 1) "orden" else "órdenes"} enviándose" else "")
        } else say("No se encuentra el Mac en la WiFi")
        _state.update { it.copy(syncing = false) }
    }

    // ---------------------------------------------------------------- Spotify playlists
    fun loadSpotifyPlaylists() = viewModelScope.launch {
        // Saved data first (shown at once and works offline); then whatever the Mac says
        if (_state.value.spotifyPlaylists == null) {
            io { cache.spotifyPlaylists() }?.let { list -> _state.update { it.copy(spotifyPlaylists = withPendingDownloads(list)) } }
        }
        _state.update { it.copy(spotifyLoading = true, spotifyError = null) }
        val offlineNote = { if (_state.value.spotifyPlaylists != null) "Sin conexión con el Mac: lista guardada" +
                                (cache.spotifySavedAt()?.let { " (${it.replace('T', ' ').dropLast(3)})" } ?: "")
                            else "No se puede contactar con el Mac" }
        val gen = generation
        val asked = System.currentTimeMillis()
        try {
            val list = io { api.spotifyPlaylists() }
            if (gen == generation) {
                io { cache.putSpotifyPlaylists(list.text); cache.reapplySince(asked) }
                _state.update { it.copy(spotifyPlaylists = withPendingDownloads(list.value)) }
            }
        } catch (e: AgentException) {
            if (e.code == 401) lostPairing()
            _state.update { it.copy(spotifyError = if (e.code == 0) offlineNote() else e.message) }
        } catch (e: Exception) {
            _state.update { it.copy(spotifyError = offlineNote()) }
        }
        _state.update { it.copy(spotifyLoading = false) }
    }

    /** Choices made on the phone that the Mac has not received yet already show as chosen. */
    private fun withPendingDownloads(list: List<com.mimusica.app.data.SpotifyPlaylist>): List<com.mimusica.app.data.SpotifyPlaylist> {
        val wanted = outbox.all().filter { it.type == "spotify_playlist" && it.isOpen && it.state != Order.CANCELLING }
            .associate { it.params.optString("id") to it.params.optBoolean("download") }
        return list.map { p -> wanted[p.id]?.let { p.copy(download = it) } ?: p }
    }

    /** Choose or drop a Spotify playlist for download (a job: works offline). */
    fun setSpotifyDownload(p: com.mimusica.app.data.SpotifyPlaylist, download: Boolean) {
        val params = JSONObject().put("id", p.id).put("name", p.name).put("download", download)
        order("spotify_playlist", params, if (download) "Descargar '${p.name}' de Spotify" else "Dejar de descargar '${p.name}'",
              quiet = true)
        _state.update { s -> s.copy(spotifyPlaylists = s.spotifyPlaylists?.map { if (it.id == p.id) it.copy(download = download) else it }) }
        say(if (download) "'${p.name}' se descargará: el Mac actualizará la biblioteca" else "'${p.name}' ya no se descarga")
    }

    fun refreshAll() = viewModelScope.launch {
        val seq = ++listSeq
        val gen = generation
        val asked = System.currentTimeMillis()
        guard {
            val (status, statusText) = io { api.status() }
            val (playlists, playlistsText) = io { api.playlists() }
            // An old agent without /reviews must not make the Mac look unreachable
            val reviewsResult = io { runCatching { api.reviews() } }
            val reviews = reviewsResult.getOrDefault(emptyList())
            // Arrived late (a newer one is on its way) or comes from the Mac before pairing/forgetting
            if (seq != listSeq || gen != generation) return@guard
            serverPlaylists = playlists
            io {
                cache.putStatus(statusText)
                cache.putPlaylists(playlistsText)
                cache.markSaved()
                cache.prunePlaylists(playlists.map { it.name })
                cache.reapplySince(asked)
            }
            // The new list arrived: drop applied deletions and edits of playlists not open
            appliedOverlay = appliedOverlay.filter { it.type == Order.EDIT && it.params.optString("name") == detailTarget }
            // If it answers, it is connected (e.g. after a "Mac no accesible")
            // Reviews decided on the phone that the Mac has not received yet are not shown again
            val decided = outbox.all().filter { it.type == Order.REVIEW && it.isOpen }.map { it.params.optString("track_id") }.toSet()
            _state.update { it.copy(status = status, connection = Connection.ONLINE, savedAt = cache.savedAt(),
                                    reviews = reviews.filter { r -> r.trackId !in decided }) }
            lastRefreshOk = true
            // Only counts as seen if the app is on screen; in the background the notification will tell
            if (reviewsResult.isSuccess && com.mimusica.app.MainActivity.visible)
                ReviewNotifier.markSeen(getApplication(), reviews.map { it.trackId })
            showWithPending()
            // Form options and the whole library (for offline search): once per session and when the library
            // changes
            val libraryKey = "${status.songs}|${status.lastRefresh}"
            if (!libraryChecked || _state.value.meta == null || io { cache.songsKey() } != libraryKey) {
                val meta = io { api.meta() }
                if (gen != generation) return@guard
                io { cache.putMeta(meta.text) }
                _state.update { it.copy(meta = meta.value) }
                // A failure here must not make the Mac look unreachable: retried on the next reload
                val library = io { runCatching { api.library() } }.getOrNull()
                if (library != null && gen == generation) io { cache.putSongs(libraryKey, library.text) }
                libraryChecked = library != null
            }
        }
    }

    // ---------------------------------------------------------------- pairing
    fun startPairing() = viewModelScope.launch {
        guard(userAction = true) {
            val id = io { api.pairStart(settings.deviceName) }
            _state.update { it.copy(pairingId = id) }
            say("Mira el código en la notificación del Mac")
        }
    }

    fun finishPairing(code: String) = viewModelScope.launch {
        val id = _state.value.pairingId ?: return@launch
        guard(userAction = true) {
            val res = io { api.pairFinish(id, code) }
            settings.token = res.getString("token")
            settings.macName = res.optString("host")
            clearSaved()   // it may be another Mac: what was saved from the previous one is no longer valid
            _state.update { it.copy(pairingId = null) }
            say("Emparejado con ${settings.macName}")
            connect()
        }
    }

    fun forgetPairing() {
        // Revoke the token on the Mac too (if reachable; otherwise it is only forgotten here). It is
        // forgotten on the phone right away: a new pairing right after is not affected
        val oldToken = settings.token
        settings.forget()
        if (oldToken != null) viewModelScope.launch { runCatching { io { api.unpair(oldToken) } } }
        socket?.close(1000, null)
        clearSaved()
        _state.update { it.copy(connection = Connection.NEEDS_PAIRING) }
    }

    private fun clearSaved() {
        generation++
        cache.clear()
        serverPlaylists = emptyList()
        serverDetail = null
        libraryChecked = false
        _state.update { it.copy(status = null, meta = null, spotifyPlaylists = null, savedAt = null) }
        showWithPending()
    }

    val macName get() = settings.macName

    // ---------------------------------------------------------------- jobs
    /** `quiet`: playlist edits, already visible on screen; no need to notify. */
    fun order(type: String, params: JSONObject, label: String, quiet: Boolean = false): Order {
        val created = outbox.add(type, params, label)
        // Watched from now on: if the Mac applies it before the next pass, it is reloaded too
        if (created.isPlaylistEdit) lastOpenEdits = lastOpenEdits + created
        SyncWorker.schedule(getApplication())
        _state.update { it.copy(orders = outbox.all().reversed()) }
        // Delivered in the background: the real state shows on the job's card
        if (!quiet) say(if (_state.value.connection == Connection.ONLINE) "Orden enviada al Mac"
                        else "Orden guardada: se enviará cuando el Mac esté accesible")
        if (_state.value.connection == Connection.ONLINE) viewModelScope.launch {
            // If the Mac actually stopped answering, say so instead of implying it arrived
            if (!discovery.ensureReachable()) {
                _state.update { it.copy(connection = Connection.NOT_FOUND) }
                say("El Mac no responde: la orden queda guardada y se enviará sola")
            }
        }
        return created
    }

    /** Cancel a job: on the phone if it has not left yet; if it is on the Mac, only while still queued. */
    fun cancelOrder(order: Order) {
        when (outbox.requestCancel(order.clientId)) {
            Order.CANCELLED -> {
                say("Orden cancelada")
                if (order.type == "spotify_playlist") {   // undo the switch's optimistic change
                    val id = order.params.optString("id"); val was = !order.params.optBoolean("download")
                    _state.update { s -> s.copy(spotifyPlaylists = s.spotifyPlaylists?.map { if (it.id == id) it.copy(download = was) else it }) }
                }
            }
            Order.CANCELLING -> {
                // It may have reached the Mac: ask it to cancel (if already started, notify and carry on)
                SyncWorker.schedule(getApplication())
                say("Cancelando en el Mac…")
            }
            else -> say("Ya no se puede cancelar: el Mac la ha terminado")
        }
        _state.update { it.copy(orders = outbox.all().reversed()) }
        showWithPending()   // a cancelled edit no longer shows as done
        if (order.type == "spotify_playlist" && _state.value.spotifyPlaylists != null) loadSpotifyPlaylists()
    }

    fun clearFinishedOrders() {
        outbox.clearFinished()
        _state.update { it.copy(orders = outbox.all().reversed()) }
    }

    // ---------------------------------------------------------------- playlists
    fun loadPlaylist(name: String) = viewModelScope.launch {
        val seq = ++loadSeq
        val gen = generation
        val asked = System.currentTimeMillis()
        detailTarget = name
        // While the Mac's one arrives (or if it does not answer) the last saved one is shown
        if (serverDetail?.summary?.name != name) {
            serverDetail = null
            val saved = io { cache.playlist(name) }
            if (seq == loadSeq && serverDetail == null) serverDetail = saved
        }
        _state.update { it.copy(detailLoading = true) }
        showWithPending()
        guard {
            // 404: no longer exists (e.g. just deleted): no error notice
            val fetched = try { io { api.playlist(name) } } catch (e: AgentException) { if (e.code == 404) null else throw e }
            if (gen != generation) return@guard
            val detail = fetched?.value
            // Only the most recent response: a slow one (for this or another playlist) does not cover the good
            // one (nor in the copy)
            if (seq == loadSeq && detailTarget == name) {
                if (fetched != null) io { cache.putPlaylist(name, fetched.text); cache.reapplySince(asked) }
                if (detail == null) {
                    say("'$name' ya no existe en el Mac")
                    detailTarget = null   // do not request it again on every reload
                }
                serverDetail = detail
                appliedOverlay = appliedOverlay.filterNot { it.type == Order.EDIT && it.params.optString("name") == name }
                showWithPending()
            }
        }
        if (seq == loadSeq) _state.update { it.copy(detailLoading = false) }
    }

    /**
     * Remove/add/delete is saved in the outbox (works without the Mac) and shown as done right away;
     * when syncing, the Mac applies it and remembers it when rebuilding the playlist.
     */
    fun removeTrack(name: String, song: Song) = editPlaylist(name, remove = listOf(song), label = "Quitar '${song.title}' de '$name'")

    /** "Undo" right after removing: back to how it was (pinned only if it was). */
    fun restoreTracks(name: String, songs: List<Song>) =
        editPlaylist(name, restore = songs, label = "Deshacer: '${songs.firstOrNull()?.title}' en '$name'")

    fun addTracks(name: String, songs: List<Song>) =
        editPlaylist(name, add = songs, label = "Añadir ${songs.singleOrNull()?.let { "'${it.title}'" } ?: "${songs.size} canciones"} a '$name'")

    private fun editPlaylist(name: String, add: List<Song> = emptyList(), remove: List<Song> = emptyList(),
                             restore: List<Song> = emptyList(), label: String) {
        val params = JSONObject().put("name", name)
            .put("add", JSONArray(add.map { it.trackId }))
            .put("remove", JSONArray(remove.map { it.trackId }))
            .put("restore", JSONArray(restore.map { it.trackId }))
            .put("songs", JSONArray((add + restore).map { it.toJson() }))   // to show them before syncing
        order(Order.EDIT, params, label, quiet = true)
        showWithPending()
    }

    fun deletePlaylist(name: String, onDone: () -> Unit) {
        order(Order.DELETE, JSONObject().put("name", name), "Eliminar '$name'", quiet = true)
        if (detailTarget == name) {   // nothing left to reload for it
            detailTarget = null
            serverDetail = null
        }
        showWithPending()
        say("'$name' eliminada" + if (_state.value.connection == Connection.ONLINE) "" else " (se aplicará en el Mac al sincronizar)")
        onDone()
    }

    /** Phone changes not yet reflected in the Mac's data, in the order they were made. */
    private fun pendingEdits() = appliedOverlay + outbox.all().filter { it.isPlaylistEdit && it.isOpen && it.state != Order.CANCELLING }

    private fun showWithPending() {
        val pending = pendingEdits()
        val detail = serverDetail?.let { d ->
            var tracks = d.tracks
            var excluded = d.excluded
            pending.filter { it.type == Order.EDIT && it.params.optString("name") == d.summary.name }.forEach { o ->
                val removed = o.params.optJSONArray("remove").strings().toSet()
                val added = o.params.optJSONArray("songs").objects().map { Song.from(it) }
                val addedIds = added.map { it.trackId }.toSet()
                excluded = excluded.filter { it.trackId !in addedIds } +
                    tracks.filter { it.trackId in removed && excluded.none { e -> e.trackId == it.trackId } }
                tracks = tracks.filter { it.trackId !in removed } + added.filter { a -> tracks.none { it.trackId == a.trackId } }
            }
            d.copy(tracks = tracks, excluded = excluded)
        }
        // Delete and create with the same name: the latest job wins. Playlists created on the phone that
        // the Mac has not made yet show in the list as pending
        val last = mutableMapOf<String, String>()
        val recreated = mutableSetOf<String>()   // deleted and created again: it is new, not the old one
        // (in the order they were made: first those already applied on the Mac, then the outbox)
        (appliedOverlay.filter { it.type == Order.DELETE } +
         outbox.all().filter { (it.type == Order.DELETE || it.type == Order.BUILD) && it.isOpen && it.state != Order.CANCELLING }
        ).forEach { o ->
            val n = o.params.optString("name").let { if (o.type == Order.BUILD) it.trim() else it }
            if (n.isEmpty()) return@forEach
            if (o.type == Order.BUILD && last[n] == Order.DELETE) recreated += n
            last[n] = o.type
        }
        // While the deletion is pending (or without a newer list from the Mac), the Mac's playlist is the
        // old one. No clock comparison between Mac and phone: at worst, the new one shows a bit later
        val isOld = { p: PlaylistSummary -> p.name in recreated }
        val known = serverPlaylists.filterNot(isOld).map { it.name }.toSet()
        val created = last.filter { (n, type) -> type == Order.BUILD && n !in known }.keys
            .map { PlaylistSummary(it, "custom", 0, false, 0, 0, null, null, emptyList(), pending = true) }
        val hidden = { p: PlaylistSummary -> last[p.name] == Order.DELETE || (last[p.name] == Order.BUILD && isOld(p)) }
        _state.update { it.copy(playlists = created + serverPlaylists.filterNot(hidden), detail = detail) }
    }

    /** Edits of this playlist waiting for the Mac to be reachable. */
    fun pendingCount(name: String) = _state.value.orders.count { it.isPlaylistEdit && it.isOpen && it.params.optString("name") == name }

    /** In the Mac's library; offline (or on failure), in the copy saved on the phone. */
    suspend fun searchLibrary(q: String): List<Song> {
        if (_state.value.connection == Connection.ONLINE) runCatching { io { api.songs(q, limit = 40) } }.onSuccess { return it }
        return io { cache.searchSongs(q, limit = 40) }
    }

    fun coverUrl(trackId: String, size: Int = 300) = api.coverUrl(trackId, size)
    fun audioUrl(trackId: String) = api.audioUrl(trackId)

    /** Approve or remove a version: shown at once and sent to the Mac as a job (offline too). */
    fun decideReview(review: com.mimusica.app.data.Review, approve: Boolean) {
        order(Order.REVIEW, JSONObject().put("track_id", review.trackId).put("decision", if (approve) "approve" else "reject"),
              (if (approve) "Aprobar " else "Quitar ") + review.song, quiet = true)
        _state.update { it.copy(reviews = it.reviews.filter { r -> r.trackId != review.trackId }) }
        say(when {
            approve -> "Aprobada: se queda en el MP3"
            review.hasPrevious -> "Quitada: vuelve la versión que había antes"
            else -> "Quitada: fuera del MP3 y de las playlists; no se volverá a descargar"
        })
    }

    // ---------------------------------------------------------------- live progress
    private fun openSocket() {
        val url = api.wsUrl() ?: return
        socketConnected = true
        socket = AgentApi.http.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                val ev = runCatching { JSONObject(text) }.getOrNull() ?: return
                when (ev.optString("event")) {
                    "job" -> {
                        val job = ev.getJSONObject("job")
                        val id = job.getString("id")
                        val lines = ev.optJSONArray("new_lines").strings()
                        _state.update { s ->
                            s.copy(liveLog = s.liveLog + (id to ((s.liveLog[id] ?: emptyList()) + lines).takeLast(60)))
                        }
                        val status = job.optString("status")
                        if (status == "done" || status == "failed") {
                            SyncWorker.schedule(getApplication())   // updates the outbox and notifies
                            refreshAll()
                        }
                    }
                    "reviews_changed" -> refreshAll()
                    "playlists_changed" -> {
                        refreshAll()
                        _state.value.detail?.summary?.name?.let { loadPlaylist(it) }
                    }
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                socketConnected = false
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                socketConnected = false
            }
        })
    }

    override fun onCleared() {
        socket?.close(1000, null)
        super.onCleared()
    }
}
