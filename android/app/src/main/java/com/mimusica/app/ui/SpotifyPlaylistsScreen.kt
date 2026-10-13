package com.mimusica.app.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.mimusica.app.data.SpotifyPlaylist
import com.mimusica.app.ui.theme.Muted

/**
 * Your Spotify playlists: turning one on downloads it into the library (and creates the local
 * playlist); turning it off removes it. Spotify only allows reading your own playlists' songs.
 */
@Composable
fun SpotifyPlaylistsScreen(state: UiState, vm: AppViewModel) {
    LaunchedEffect(Unit) { vm.loadSpotifyPlaylists() }
    val list = state.spotifyPlaylists
    LazyColumn(Modifier.fillMaxWidth()) {
        item {
            Text("Activa las que quieras tener en tus MP3: se descargan en la próxima actualización y su " +
                 "playlist se mantiene igual que en Spotify. Las de otras personas no se pueden descargar.",
                 color = Muted, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(16.dp))
            if (state.spotifyLoading) LinearProgressIndicator(Modifier.fillMaxWidth().padding(horizontal = 16.dp))
            state.spotifyError?.let {
                Row(Modifier.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(it, color = MaterialTheme.colorScheme.error, modifier = Modifier.weight(1f))
                    TextButton(onClick = { vm.loadSpotifyPlaylists() }) { Text("Reintentar") }
                }
            }
        }
        val sorted = list.orEmpty().sortedWith(compareByDescending<SpotifyPlaylist> { it.download }
            .thenByDescending { it.downloadable && !it.fromApp }.thenBy { it.name.lowercase() })
        items(sorted, key = { it.id }) { p -> SpotifyRow(p, vm) }
    }
}

@Composable
private fun SpotifyRow(p: SpotifyPlaylist, vm: AppViewModel) {
    val canToggle = (p.downloadable && !p.fromApp) || p.download
    Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp).alpha(if (canToggle) 1f else 0.5f),
        verticalAlignment = Alignment.CenterVertically) {
        CoverImage(p.image, p.name, Modifier.size(52.dp), RoundedCornerShape(6.dp))
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(p.name, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
            val detail = listOfNotNull(
                "${p.tracks} canciones",
                if (p.mine) null else "de ${p.owner}",
                when {
                    p.fromApp -> "creada por Mi Música"
                    !p.downloadable -> "Spotify no deja descargarla"
                    p.status == "unavailable" -> "no disponible en Spotify"
                    p.download && p.localName != null -> "en tu biblioteca como '${p.localName}'"
                    p.download -> "se descargará en la próxima actualización"
                    else -> null
                },
            ).joinToString(" · ")
            Text(detail, color = Muted, style = MaterialTheme.typography.bodySmall, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        Switch(checked = p.download, enabled = canToggle, onCheckedChange = { vm.setSpotifyDownload(p, it) })
    }
}
