package com.mimusica.app.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.LocalDateTime

/**
 * Phone copy of the last thing the Mac said (status, playlists and their detail, form options,
 * library and Spotify playlists): offline they can be viewed and edited, and jobs go to the outbox
 * as usual. The response is saved as is (JSON text) and read with the same `parse*` functions as
 * the Mac's responses. Edits the Mac already applied (including those SyncWorker delivers with the
 * app closed) are applied here with `apply*`, so the old state does not reappear offline.
 */
class LocalCache(context: Context) {
    private val dir = File(context.applicationContext.filesDir, "offline").apply { mkdirs() }
    private val details = File(dir, "playlists").apply { mkdirs() }

    private companion object { val LOCK = Any() }

    private fun write(file: File, text: String) = synchronized(LOCK) {
        // Atomic write: an interruption never leaves a truncated JSON
        runCatching {
            file.parentFile?.mkdirs()
            val tmp = File(file.path + ".tmp")
            tmp.writeText(text)
            if (!tmp.renameTo(file)) { file.delete(); tmp.renameTo(file) }
        }
    }

    private fun read(file: File): String? = synchronized(LOCK) { runCatching { file.readText() }.getOrNull() }

    /** Reads and parses; a corrupt file or one from an old version counts as "no data". */
    private fun <T> load(file: File, parse: (String) -> T): T? = read(file)?.let { runCatching { parse(it) }.getOrNull() }

    private fun detailFile(name: String): File {
        val hash = MessageDigest.getInstance("SHA-1").digest(name.toByteArray()).joinToString("") { "%02x".format(it) }
        return File(details, "$hash.json")
    }

    // ---------------------------------------------------------------- save (Mac responses)
    private fun now() = LocalDateTime.now().withNano(0).toString()

    fun putStatus(text: String) = write(File(dir, "status.json"), text)
    fun putPlaylists(text: String) = write(File(dir, "playlists.json"), text)
    fun putPlaylist(name: String, text: String) = write(detailFile(name), text)
    fun putMeta(text: String) = write(File(dir, "meta.json"), text)
    fun putSpotifyPlaylists(text: String) { write(File(dir, "spotify_playlists.json"), text); write(File(dir, "spotify_saved_at"), now()) }

    /** Called when a whole reload (status + playlist list) succeeded. */
    fun markSaved() = write(File(dir, "saved_at"), now())
    fun putSongs(key: String, text: String) { write(File(dir, "songs.json"), text); write(File(dir, "songs_key"), key) }

    // ---------------------------------------------------------------- read
    fun status() = load(File(dir, "status.json"), AgentApi::parseStatus)
    fun playlists() = load(File(dir, "playlists.json"), AgentApi::parsePlaylists)
    fun playlist(name: String) = load(detailFile(name), AgentApi::parsePlaylist)?.takeIf { it.summary.name == name }
    fun meta() = load(File(dir, "meta.json"), AgentApi::parseMeta)
    fun spotifyPlaylists() = load(File(dir, "spotify_playlists.json"), AgentApi::parseSpotifyPlaylists)
    fun songs() = load(File(dir, "songs.json"), AgentApi::parseSongs)
    fun songsKey(): String? = read(File(dir, "songs_key"))
    fun hasPlaylist(name: String) = detailFile(name).exists()

    /** When a full reload from the Mac was last saved ("2026-10-12T18:30:00"), or null. */
    fun savedAt(): String? = read(File(dir, "saved_at"))?.trim()?.takeIf { it.isNotEmpty() }
    fun spotifySavedAt(): String? = read(File(dir, "spotify_saved_at"))?.trim()?.takeIf { it.isNotEmpty() }

    // ---------------------------------------------------------------- jobs already applied on the Mac
    /** Reads, changes and saves a JSON; does nothing if it is missing or corrupt. */
    private fun <T> modify(file: File, parse: (String) -> T, change: (T) -> Unit) = synchronized(LOCK) {
        val value = read(file)?.let { runCatching { parse(it) }.getOrNull() } ?: return@synchronized
        change(value)
        write(file, value.toString())
    }

    private fun JSONArray.filtered(keep: (JSONObject) -> Boolean) = JSONArray(objects().filter(keep))

    /** edit_playlist done: the same the app shows with pending edits (AppViewModel.showWithPending). */
    fun applyEdit(params: JSONObject) = synchronized(LOCK) {
        val name = params.optString("name")
        val removed = params.optJSONArray("remove").strings().toSet()
        val added = params.optJSONArray("songs").objects()
        val addedIds = added.map { it.optString("track_id") }.toSet()
        var count: Int? = null
        modify(detailFile(name), ::JSONObject) { o ->
            val tracks = o.optJSONArray("tracks") ?: JSONArray()
            val excluded = o.optJSONArray("excluded") ?: JSONArray()
            val present = tracks.objects().map { it.optString("track_id") }.toSet()
            val newExcluded = excluded.filtered { it.optString("track_id") !in addedIds }
            val excludedIds = excluded.objects().map { it.optString("track_id") }.toSet()
            tracks.objects().filter { it.optString("track_id") in removed && it.optString("track_id") !in excludedIds }
                .forEach { newExcluded.put(it) }
            val newTracks = tracks.filtered { it.optString("track_id") !in removed }
            added.filter { it.optString("track_id") !in present }.forEach { newTracks.put(it) }
            count = newTracks.length()
            o.put("tracks", newTracks).put("excluded", newExcluded).put("count", newTracks.length())
            if (removed.isNotEmpty() || addedIds.isNotEmpty()) o.put("edited", true)
        }
        modify(File(dir, "playlists.json"), ::JSONArray) { list ->
            list.objects().filter { it.optString("name") == name }.forEach { p ->
                count?.let { p.put("count", it) }
                if (removed.isNotEmpty() || addedIds.isNotEmpty()) p.put("edited", true)
            }
        }
    }

    /** delete_playlist done. */
    fun applyDelete(name: String) = synchronized(LOCK) {
        detailFile(name).delete()
        val file = File(dir, "playlists.json")
        val list = read(file)?.let { runCatching { JSONArray(it) }.getOrNull() } ?: return@synchronized
        write(file, list.filtered { it.optString("name") != name }.toString())
        // If it was downloaded from Spotify, the Mac also stops downloading it
        modify(File(dir, "spotify_playlists.json"), ::JSONArray) { sp ->
            sp.objects().filter { it.optString("local_name") == name }.forEach { it.put("download", false) }
        }
    }

    /**
     * Jobs applied by SyncWorker: a Mac response requested before they were applied but saved after
     * would undo them, so the ones after that request are applied again (applying twice is harmless).
     */
    fun apply(type: String, params: JSONObject, remember: Boolean = true) = synchronized(LOCK) {
        when (type) {
            Order.EDIT -> applyEdit(params)
            Order.DELETE -> applyDelete(params.optString("name"))
            "spotify_playlist" -> applySpotifyDownload(params.optString("id"), params.optBoolean("download"))
            Order.BUILD -> {}   // does not change the copy, but the earlier deletion with that name is not repeated
            else -> return@synchronized
        }
        if (!remember) return@synchronized
        val file = File(dir, "applied.json")
        val list = read(file)?.let { runCatching { JSONArray(it) }.getOrNull() } ?: JSONArray()
        val name = params.optString("name").trim()
        val kept = if (type != Order.BUILD) list.objects()
                   else list.objects().filterNot { it.optString("type") == Order.DELETE && it.optJSONObject("params")?.optString("name") == name }
        if (type == Order.BUILD) { write(file, JSONArray(kept).toString()); return@synchronized }
        val recent = kept.takeLast(49) + JSONObject().put("type", type).put("params", params)
            .put("at", System.currentTimeMillis())
        write(file, JSONArray(recent).toString())
    }

    /** `since`: when the just-saved response was requested; what was applied before is already in it. */
    fun reapplySince(since: Long) = synchronized(LOCK) {
        val list = read(File(dir, "applied.json"))?.let { runCatching { JSONArray(it) }.getOrNull() } ?: return@synchronized
        list.objects().filter { it.optLong("at") >= since }.forEach {
            apply(it.optString("type"), it.optJSONObject("params") ?: JSONObject(), remember = false)
        }
    }

    /** spotify_playlist done: the switch stays as chosen. */
    fun applySpotifyDownload(id: String, download: Boolean) =
        modify(File(dir, "spotify_playlists.json"), ::JSONArray) { list ->
            list.objects().filter { it.optString("id") == id }.forEach { it.put("download", download) }
        }

    /** Drops the detail of playlists that no longer exist on the Mac. */
    fun prunePlaylists(names: Collection<String>) = synchronized(LOCK) {
        val keep = names.map { detailFile(it).name }.toSet()
        details.listFiles()?.filter { it.name !in keep }?.forEach { it.delete() }
    }

    /** When pairing another Mac (or forgetting this one), what was saved is no longer valid. */
    fun clear() = synchronized(LOCK) {
        dir.deleteRecursively()
        details.mkdirs()
    }

    /**
     * Offline search, same as the Mac's (/songs): every word in "artist title", sorted by artist and
     * title.
     */
    fun searchSongs(q: String, limit: Int): List<Song> {
        val words = q.lowercase().split(Regex("\\s+")).filter { it.isNotEmpty() }
        return songs().orEmpty()
            .filter { s -> val text = "${s.artist} ${s.title}".lowercase(); words.all { it in text } }
            .distinctBy { it.trackId }
            .sortedWith(compareBy<Song> { it.artist }.thenBy { it.title })
            .take(limit)
    }
}

