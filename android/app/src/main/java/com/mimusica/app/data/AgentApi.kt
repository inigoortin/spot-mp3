package com.mimusica.app.data

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Parsed response and its text (to save it in LocalCache only if it is still current). */
data class Fetched<T>(val value: T, val text: String)

/** Agent error with its HTTP status (401 = not paired, 404 = not found...). */
class AgentException(val code: Int, message: String) : IOException(message)

/** HTTP client for the Mac agent. Every call is blocking: use it from Dispatchers.IO or a Worker. */
class AgentApi(private val settings: Settings) {

    companion object {
        val http: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(4, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .pingInterval(20, TimeUnit.SECONDS) // keeps the WebSocket alive
            .build()
        private val JSON = "application/json".toMediaType()

        // Response parsing; LocalCache also uses it to read what it saved
        fun parseStatus(text: String): AgentStatus {
            val o = JSONObject(text)
            val last = o.optJSONObject("last_refresh")
            return AgentStatus(
                host = o.optString("host"), songs = o.optInt("songs"), playlists = o.optInt("playlists"),
                queued = o.optInt("queued"), running = o.optJSONObject("running")?.let { Job.from(it) },
                lastRefresh = last?.optStringOrNull("when"), lastRefreshLine = last?.optStringOrNull("last_line"),
                pendingReviews = o.optInt("pending_reviews"),
            )
        }

        fun parseMeta(text: String): Meta = Meta.from(JSONObject(text))

        fun parseSongs(text: String): List<Song> = JSONObject(text).getJSONArray("songs").objects().map { Song.from(it) }

        fun parsePlaylists(text: String): List<PlaylistSummary> = JSONArray(text).objects().map { PlaylistSummary.from(it) }

        fun parsePlaylist(text: String): PlaylistDetail {
            val o = JSONObject(text)
            return PlaylistDetail(PlaylistSummary.from(o), o.getJSONArray("tracks").objects().map { Song.from(it) },
                                  o.optJSONArray("excluded").objects().map { Song.from(it) })
        }

        fun parseSpotifyPlaylists(text: String): List<SpotifyPlaylist> =
            JSONArray(text).objects().map { SpotifyPlaylist.from(it) }
    }

    private fun base(): String = settings.baseUrl ?: throw AgentException(0, "Mac no encontrado")

    /** Each segment is encoded separately: playlist names contain spaces, accents, "+"... */
    private fun url(segments: List<String>, query: Map<String, String>) =
        base().toHttpUrl().newBuilder().apply {
            segments.forEach { addPathSegment(it) }
            query.forEach { (k, v) -> addQueryParameter(k, v) }
        }.build()

    private fun call(method: String, path: String, body: JSONObject? = null,
                     query: Map<String, String> = emptyMap(), auth: Boolean = true,
                     segments: List<String> = path.trim('/').split('/')): String {
        val builder = Request.Builder().url(url(segments, query))
        if (auth) settings.token?.let { builder.header("Authorization", "Bearer $it") }
        val reqBody = body?.toString()?.toRequestBody(JSON)
        when (method) {
            "GET" -> builder.get()
            "POST" -> builder.post(reqBody ?: "{}".toRequestBody(JSON))
            "DELETE" -> builder.delete()
        }
        http.newCall(builder.build()).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                val detail = runCatching { JSONObject(text).optString("detail") }.getOrNull()
                throw AgentException(resp.code, detail?.takeIf { it.isNotEmpty() } ?: "Error ${resp.code}")
            }
            return text
        }
    }

    // ---------------------------------------------------------------- connection
    fun ping(): JSONObject = JSONObject(call("GET", "/ping", auth = false))

    /** true only if the spot-mp3 agent answers (not just any service at that address). */
    fun isAgent(): Boolean = runCatching { ping().optString("app") == "spot-mp3-agent" }.getOrDefault(false)

    /** Revokes `oldToken` on the Mac (stops working), even if the phone already forgot it. */
    fun unpair(oldToken: String) {
        val req = Request.Builder().url(url(listOf("pair"), emptyMap())).delete()
            .header("Authorization", "Bearer $oldToken").build()
        http.newCall(req).execute().close()
    }

    fun pairStart(device: String): String =
        JSONObject(call("POST", "/pair/start", JSONObject().put("device", device), auth = false)).getString("pairing_id")

    fun pairFinish(pairingId: String, code: String): JSONObject =
        JSONObject(call("POST", "/pair/finish", JSONObject().put("pairing_id", pairingId).put("code", code), auth = false))

    // ---------------------------------------------------------------- data
    // They also return the text: the caller decides whether to save it (not if the response arrived late)
    fun status(): Fetched<AgentStatus> = call("GET", "/status").let { Fetched(parseStatus(it), it) }

    fun meta(): Fetched<Meta> = call("GET", "/meta").let { Fetched(parseMeta(it), it) }

    fun songs(q: String, genre: String = "", limit: Int = 50): List<Song> =
        parseSongs(call("GET", "/songs", query = mapOf("q" to q, "genre" to genre, "limit" to "$limit")))

    /** The whole library, for offline search. */
    fun library(): Fetched<List<Song>> = call("GET", "/songs", query = mapOf("limit" to "100000")).let { Fetched(parseSongs(it), it) }

    fun playlists(): Fetched<List<PlaylistSummary>> = call("GET", "/playlists").let { Fetched(parsePlaylists(it), it) }

    fun playlist(name: String): Fetched<PlaylistDetail> =
        call("GET", "", segments = listOf("playlists", name)).let { Fetched(parsePlaylist(it), it) }
    // Editing and deleting playlists goes through the outbox (edit_playlist / delete_playlist jobs)

    // ---------------------------------------------------------------- jobs
    fun submitJob(clientId: String, type: String, params: JSONObject): Job =
        Job.from(JSONObject(call("POST", "/jobs", JSONObject().put("client_id", clientId).put("type", type).put("params", params))))

    /** Cancels on the Mac a job still queued (409 if already running or finished). */
    fun cancelJob(idOrClientId: String): Job =
        Job.from(JSONObject(call("POST", "", segments = listOf("jobs", idOrClientId, "cancel"))))

    fun job(idOrClientId: String): Job = Job.from(JSONObject(call("GET", "", segments = listOf("jobs", idOrClientId))))

    fun jobs(limit: Int = 30): List<Job> = JSONArray(call("GET", "/jobs", query = mapOf("limit" to "$limit"))).objects().map { Job.from(it) }

    // ---------------------------------------------------------------- covers and WebSocket
    fun coverUrl(trackId: String, size: Int = 300): String? =
        settings.baseUrl?.let { "$it/cover/$trackId?size=$size&token=${settings.token}" }

    fun spotifyPlaylists(): Fetched<List<SpotifyPlaylist>> =
        call("GET", "/spotify/playlists").let { Fetched(parseSpotifyPlaylists(it), it) }

    fun reviews(): List<Review> = JSONArray(call("GET", "/reviews")).objects().map { Review.from(it) }

    fun audioUrl(trackId: String): String? = settings.baseUrl?.let { "$it/audio/$trackId?token=${settings.token}" }

    fun wsUrl(): String? = settings.host?.let { "ws://$it:${settings.port}/ws?token=${settings.token}" }
}
