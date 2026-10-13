package com.mimusica.app.ui

import android.media.AudioAttributes
import android.media.MediaPlayer
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.mimusica.app.data.Review
import com.mimusica.app.ui.theme.Danger
import com.mimusica.app.ui.theme.Muted

/**
 * Versions different from Spotify's: streamed from the Mac to listen to them, then approved (kept)
 * or removed (the previous one comes back if there was one; otherwise out of the MP3 and playlists).
 */
@Composable
fun ReviewsScreen(state: UiState, vm: AppViewModel) {
    val player = remember { ReviewPlayer() }
    DisposableEffect(Unit) { onDispose { player.release() } }   // stops when leaving the screen
    val lifecycle = androidx.lifecycle.compose.LocalLifecycleOwner.current
    DisposableEffect(lifecycle) {   // and also when the app goes to the background
        val obs = androidx.lifecycle.LifecycleEventObserver { _, e -> if (e == androidx.lifecycle.Lifecycle.Event.ON_STOP) player.release() }
        lifecycle.lifecycle.addObserver(obs)
        onDispose { lifecycle.lifecycle.removeObserver(obs) }
    }
    LaunchedEffect(Unit) { vm.refreshAll() }

    LazyColumn(contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            Text("Estas canciones no están en YouTube Music en la misma versión que en Spotify y se ha " +
                 "descargado otra. Escúchalas: si te gusta, apruébala; si no, quítala (vuelve la versión anterior si la " +
                 "había; si no, sale del MP3 y no se volverá a descargar). " +
                 "También desde el Mac: python version_review.py",
                 color = Muted, style = MaterialTheme.typography.bodyMedium)
        }
        if (state.reviews.isEmpty()) item {
            Text(if (state.connection == Connection.ONLINE) "No hay nada para revisar." else "Conecta con el Mac para ver las versiones.",
                 color = Muted, modifier = Modifier.padding(top = 24.dp))
        }
        items(state.reviews, key = { it.trackId }) { r ->
            ReviewCard(r, vm, player, onDecide = { approve -> player.stopIf(r.trackId); vm.decideReview(r, approve) })
        }
    }
}

@Composable
private fun ReviewCard(r: Review, vm: AppViewModel, player: ReviewPlayer, onDecide: (Boolean) -> Unit) {
    Card(shape = RoundedCornerShape(14.dp)) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CoverImage(vm.coverUrl(r.trackId, 120), r.song, Modifier.size(56.dp))
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(r.song, fontWeight = FontWeight.SemiBold, maxLines = 2, overflow = TextOverflow.Ellipsis)
                    Text(r.reason, color = Muted, style = MaterialTheme.typography.bodySmall)
                }
                if (r.playable) {
                    val playing = player.current == r.trackId
                    FilledTonalIconButton(onClick = { player.toggle(r.trackId, vm.audioUrl(r.trackId)) }) {
                        when {
                            playing && player.preparing -> CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                            playing -> Icon(Icons.Filled.Pause, "Parar")
                            else -> Icon(Icons.Filled.PlayArrow, "Escuchar")
                        }
                    }
                }
            }
            Row(Modifier.fillMaxWidth().padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { onDecide(true) }, modifier = Modifier.weight(1f)) {
                    Icon(Icons.Filled.Check, null, Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)); Text("Aprobar")
                }
                OutlinedButton(onClick = { onDecide(false) }, modifier = Modifier.weight(1f),
                               colors = ButtonDefaults.outlinedButtonColors(contentColor = Danger)) {
                    Icon(Icons.Filled.Delete, null, Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)); Text("Quitar")
                }
            }
        }
    }
}

/** A single player: tapping another song stops the previous one. */
private class ReviewPlayer {
    private var mp: MediaPlayer? = null
    var current by mutableStateOf<String?>(null)
        private set
    var preparing by mutableStateOf(false)
        private set

    fun toggle(trackId: String, url: String?) {
        val wasThis = current == trackId
        release()
        if (wasThis || url == null) return
        current = trackId
        preparing = true
        mp = try {
            MediaPlayer().apply {
                setAudioAttributes(AudioAttributes.Builder().setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
                setDataSource(url)
                setOnPreparedListener { preparing = false; it.start() }
                setOnCompletionListener { release() }
                setOnErrorListener { _, _, _ -> release(); true }
                prepareAsync()
            }
        } catch (e: Exception) {   // Invalid URL or Mac unreachable: nothing plays, without closing the app
            current = null
            preparing = false
            null
        }
    }

    fun stopIf(trackId: String) { if (current == trackId) release() }

    fun release() {
        mp?.runCatching { stop() }
        mp?.release()
        mp = null
        current = null
        preparing = false
    }
}
