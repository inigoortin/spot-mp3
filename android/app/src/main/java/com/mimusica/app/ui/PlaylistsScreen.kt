package com.mimusica.app.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.Color
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.mimusica.app.data.PlaylistSummary
import com.mimusica.app.ui.theme.Accent
import com.mimusica.app.ui.theme.Muted
import com.mimusica.app.ui.theme.Warning

/** Filters of the chip row (like Spotify's "All · Music · Podcasts", adapted to playlists). */
private enum class PlaylistFilter(val label: String, val test: (PlaylistSummary) -> Boolean) {
    ALL("Todas", { true }),
    MINE("Mías", { it.source != "preset" }),
    EXAMPLES("De ejemplo", { it.source == "preset" }),
    EDITED("Personalizadas", { it.edited }),
    SPOTIFY("En Spotify", { it.spotifyUrl != null }),
    FROM_SPOTIFY("Bajadas de Spotify", { it.source == "spotify" }),
}

@Composable
fun PlaylistsScreen(state: UiState, vm: AppViewModel, onOpenSpotify: () -> Unit = {}, onOpen: (String) -> Unit) {
    var filter by rememberSaveable { mutableStateOf(PlaylistFilter.ALL) }
    val shown = state.playlists.filter(filter.test)
    Column {
    androidx.compose.material3.OutlinedButton(onClick = onOpenSpotify,
        modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp).fillMaxWidth()) {
        Text("Playlists de mi Spotify (elegir cuáles descargar)")
    }
    if (state.playlists.isNotEmpty()) LazyRow(
        contentPadding = PaddingValues(horizontal = 16.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        // Only filters with some playlist (plus "All" and the selected one, to be able to go back)
        items(PlaylistFilter.entries.filter { it == PlaylistFilter.ALL || it == filter || state.playlists.any(it.test) }) { f ->
            FilterChip(
                selected = f == filter,
                onClick = { filter = if (f == filter) PlaylistFilter.ALL else f },
                label = { Text(if (f == PlaylistFilter.ALL) f.label else "${f.label} · ${state.playlists.count(f.test)}") },
                shape = RoundedCornerShape(50),
                colors = FilterChipDefaults.filterChipColors(
                    selectedContainerColor = Accent, selectedLabelColor = Color(0xFF052E16),
                ),
            )
        }
    }
    LazyVerticalGrid(
        columns = GridCells.Adaptive(160.dp),
        contentPadding = PaddingValues(16.dp),
        horizontalArrangement = Arrangement.spacedBy(14.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        if (shown.isEmpty()) item(span = { GridItemSpan(maxLineSpan) }) {
            Text(if (state.connection == Connection.ONLINE) "No hay playlists todavía. Créalas en la pestaña Crear."
                 else "Conecta con el Mac una vez para ver tus playlists (después se guardan en el móvil).", color = Muted)
        }
        if (shown.isEmpty() && state.playlists.isNotEmpty()) item(span = { GridItemSpan(maxLineSpan) }) {
            Text("Ninguna playlist en \"${filter.label}\".", color = Muted)
        }
        items(shown, key = { it.name }) { p ->
            PlaylistCard(p, vm) {
                if (p.pending) vm.say("'${p.name}' se creará cuando el Mac esté accesible") else onOpen(p.name)
            }
        }
    }
    }
}

@Composable
private fun PlaylistCard(p: PlaylistSummary, vm: AppViewModel, onClick: () -> Unit) {
    Column(Modifier.clickable(onClick = onClick)) {
        CoverMosaic(p.coverIds, p.name, { vm.coverUrl(it, 200) }, Modifier.fillMaxWidth())
        Spacer(Modifier.height(8.dp))
        Text(p.name, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(if (p.pending) "Se creará al sincronizar" else "${p.count} canciones" + if (p.source == "preset") " · de ejemplo" else "",
                 color = Muted, style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f),
                 maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        Row(Modifier.padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            if (p.pending) Pill("Pendiente", Warning)
            if (p.spotifyUrl != null) Pill("En Spotify", Accent)
            if (p.edited) Pill("Personalizada", Warning)
            if (p.spotifyUrl == null && !p.edited && !p.pending) Spacer(Modifier.width(1.dp).height(19.dp))
        }
    }
}
