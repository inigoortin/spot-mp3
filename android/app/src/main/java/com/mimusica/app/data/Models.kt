package com.mimusica.app.data

import org.json.JSONArray
import org.json.JSONObject

/** Data returned by the Mac agent (agent_server.py). */

data class Song(
    val trackId: String,
    val artist: String,
    val title: String,
    val genre: String? = null,
    val energy: Double? = null,
    val valence: Double? = null,
    val danceability: Double? = null,
    val bpm: Double? = null,
    val camelot: String? = null,
) {
    /** The minimum to show it while an edit waits to sync with the Mac. */
    fun toJson(): JSONObject = JSONObject().put("track_id", trackId).put("artist", artist).put("title", title)
        .put("genre", genre).put("bpm", bpm).put("energy", energy)

    companion object {
        fun from(o: JSONObject) = Song(
            trackId = o.getString("track_id"),
            artist = o.optString("artist"),
            title = o.optString("title"),
            genre = o.optStringOrNull("genre"),
            energy = o.optDoubleOrNull("energy"),
            valence = o.optDoubleOrNull("valence"),
            danceability = o.optDoubleOrNull("danceability"),
            bpm = o.optDoubleOrNull("bpm"),
            camelot = o.optStringOrNull("camelot"),
        )
    }
}

data class PlaylistSummary(
    val name: String,
    val source: String?,           // "preset" | "custom" | "spotify"
    val count: Int,
    val edited: Boolean,           // has songs removed or added by hand
    val excludedCount: Int,
    val pinnedCount: Int,
    val spotifyUrl: String?,
    val updated: String?,
    val coverIds: List<String>,
    /** Created on the phone offline: still only a job in the outbox. */
    val pending: Boolean = false,
) {
    companion object {
        fun from(o: JSONObject) = PlaylistSummary(
            name = o.getString("name"),
            source = o.optStringOrNull("source"),
            count = o.optInt("count"),
            edited = o.optBoolean("edited"),
            excludedCount = o.optInt("excluded_count"),
            pinnedCount = o.optInt("pinned_count"),
            spotifyUrl = o.optStringOrNull("spotify_url"),
            updated = o.optStringOrNull("updated"),
            coverIds = o.optJSONArray("cover_ids").strings(),
        )
    }
}

/** `excluded`: songs removed by hand; they do not come back on rebuild unless restored. */
data class PlaylistDetail(val summary: PlaylistSummary, val tracks: List<Song>, val excluded: List<Song> = emptyList())

data class Job(
    val id: String,
    val clientId: String,
    val type: String,
    val status: String,            // queued | running | done | failed
    val params: JSONObject,
    val created: String?,
    val finished: String?,
    val logTail: List<String>,
    val result: JSONObject?,
) {
    val isActive get() = status == "queued" || status == "running"

    companion object {
        fun from(o: JSONObject) = Job(
            id = o.getString("id"),
            clientId = o.optString("client_id"),
            type = o.optString("type"),
            status = o.optString("status"),
            params = o.optJSONObject("params") ?: JSONObject(),
            created = o.optStringOrNull("created"),
            finished = o.optStringOrNull("finished"),
            logTail = o.optJSONArray("log_tail").strings(),
            result = o.optJSONObject("result"),
        )
    }
}

data class AgentStatus(
    val host: String,
    val songs: Int,
    val playlists: Int,
    val queued: Int,
    val running: Job?,
    val lastRefresh: String?,
    val lastRefreshLine: String?,
    val pendingReviews: Int = 0,
)


data class Meta(
    val genres: List<Pair<String, Int>>,
    val groups: Map<String, List<String>>,
    val metrics: Map<String, ClosedFloatingPointRange<Float>>,
    val orderBy: List<String>,
    val artists: List<Pair<String, Int>>,
    /** Example playlists: their names cannot be used when creating (playlist_store.name_problem). */
    val presets: List<String> = emptyList(),
) {
    companion object {
        fun from(o: JSONObject): Meta {
            val groups = mutableMapOf<String, List<String>>()
            o.getJSONObject("groups").let { g -> g.keys().forEach { groups[it] = g.getJSONArray(it).strings() } }
            val metrics = mutableMapOf<String, ClosedFloatingPointRange<Float>>()
            o.getJSONObject("metrics").let { m ->
                m.keys().forEach { k ->
                    val r = m.getJSONObject(k)
                    metrics[k] = r.getDouble("min").toFloat()..r.getDouble("max").toFloat()
                }
            }
            return Meta(
                genres = o.getJSONArray("genres").objects().map { it.getString("name") to it.getInt("count") },
                groups = groups,
                metrics = metrics,
                orderBy = o.optJSONArray("order_by").strings(),
                artists = o.getJSONArray("artists").objects().map { it.getString("name") to it.getInt("count") },
                presets = o.optJSONArray("presets").strings(),
            )
        }
    }
}

// ---------------------------------------------------------------- JSON helpers
fun JSONArray?.strings(): List<String> =
    if (this == null) emptyList() else (0 until length()).map { getString(it) }

fun JSONArray?.objects(): List<JSONObject> =
    if (this == null) emptyList() else (0 until length()).map { getJSONObject(it) }

fun JSONObject.optStringOrNull(key: String): String? =
    if (!has(key) || isNull(key)) null else optString(key).takeIf { it.isNotEmpty() }

fun JSONObject.optDoubleOrNull(key: String): Double? =
    if (!has(key) || isNull(key)) null else optDouble(key).takeIf { !it.isNaN() }

/** Version different from Spotify's, pending approval or removal. */
data class Review(val trackId: String, val song: String, val reason: String, val video: String?, val playable: Boolean,
                  val hasPrevious: Boolean = false) {
    companion object {
        fun from(o: JSONObject) = Review(
            trackId = o.getString("track_id"), song = o.optString("song"), reason = o.optString("reason"),
            video = o.optString("video").takeIf { it.isNotEmpty() && it != "null" }, playable = o.optBoolean("playable"),
            hasPrevious = o.optBoolean("has_previous"),
        )
    }
}

/** A playlist in your Spotify account; `download` = downloaded into the library (only your own can be). */
data class SpotifyPlaylist(
    val id: String, val name: String, val owner: String, val mine: Boolean, val downloadable: Boolean,
    val tracks: Int, val image: String?, val fromApp: Boolean, val download: Boolean, val status: String?,
    val localName: String?,
) {
    companion object {
        fun from(o: JSONObject) = SpotifyPlaylist(
            id = o.getString("id"), name = o.optString("name"), owner = o.optString("owner"),
            mine = o.optBoolean("mine"), downloadable = o.optBoolean("downloadable", o.optBoolean("mine")),
            tracks = o.optInt("tracks"), image = o.optStringOrNull("image"), fromApp = o.optBoolean("from_app"),
            download = o.optBoolean("download"), status = o.optStringOrNull("status"),
            localName = o.optStringOrNull("local_name"),
        )
    }
}
