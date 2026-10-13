package com.mimusica.app.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.OpenInNew
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.CloudUpload
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.RemoveCircleOutline
import androidx.compose.material.icons.filled.Restore
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.mimusica.app.data.Song
import com.mimusica.app.ui.theme.Accent
import com.mimusica.app.ui.theme.Danger
import com.mimusica.app.ui.theme.Muted
import com.mimusica.app.ui.theme.Warning
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PlaylistDetailScreen(name: String, state: UiState, vm: AppViewModel, onBack: () -> Unit) {
    LaunchedEffect(name) { vm.loadPlaylist(name) }
    val detail = state.detail?.takeIf { it.summary.name == name }
    val context = LocalContext.current
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    var confirmPublish by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }
    var showAdd by remember { mutableStateOf(false) }
    var showExcluded by remember { mutableStateOf(false) }
    val pending = vm.pendingCount(name)

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(name, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Atrás") } },
                actions = { IconButton(onClick = { confirmDelete = true }) { Icon(Icons.Filled.Delete, "Eliminar playlist") } },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
                windowInsets = WindowInsets(0),   // the main screen already leaves room for the status bar
            )
        },
        contentWindowInsets = WindowInsets(0),
        floatingActionButton = {
            ExtendedFloatingActionButton(onClick = { showAdd = true }, icon = { Icon(Icons.Filled.Add, null) },
                                         text = { Text("Añadir") })
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        LazyColumn(Modifier.padding(padding).fillMaxSize()) {
            item {
                Header(name, detail?.summary?.coverIds ?: emptyList(), vm,
                    subtitle = detail?.let { "${it.tracks.size} canciones · En tus MP3" } ?: "",
                    note = if (pending > 0) "$pending ${if (pending == 1) "cambio espera" else "cambios esperan"} a sincronizar con el Mac" else null,
                )
                Row(Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Button(onClick = { confirmPublish = true }, enabled = detail != null) {
                        Icon(Icons.Filled.CloudUpload, null, Modifier.size(18.dp))
                        Spacer(Modifier.width(6.dp))
                        Text(if (detail?.summary?.spotifyUrl != null) "Actualizar en Spotify" else "Publicar en Spotify")
                    }
                    detail?.summary?.spotifyUrl?.let { url ->
                        OutlinedButton(onClick = { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }) {
                            Icon(Icons.AutoMirrored.Filled.OpenInNew, null, Modifier.size(18.dp))
                            Spacer(Modifier.width(6.dp))
                            Text("Abrir")
                        }
                    }
                }
                if (state.detailLoading && detail == null) LinearProgressIndicator(Modifier.fillMaxWidth().padding(16.dp))
                else if (detail == null) Text(if (state.connection == Connection.ONLINE) "Esta playlist ya no existe en el Mac (o no se ha podido cargar)."
                                              else "Sin conexión: esta playlist no está guardada en el móvil. Ábrela una vez con el Mac accesible.",
                                             color = Muted, modifier = Modifier.padding(16.dp))
                Spacer(Modifier.height(8.dp))
            }
            items(detail?.tracks ?: emptyList(), key = { it.trackId }) { song ->
                SongRow(song, vm) {
                    IconButton(onClick = {
                        vm.removeTrack(name, song)
                        scope.launch {
                            snackbar.currentSnackbarData?.dismiss()
                            val r = snackbar.showSnackbar("Quitada: ${song.title}. No volverá al regenerarla.", actionLabel = "Deshacer")
                            if (r == SnackbarResult.ActionPerformed) vm.restoreTracks(name, listOf(song))
                        }
                    }) { Icon(Icons.Filled.RemoveCircleOutline, "Quitar", tint = Muted) }
                }
            }
            // Removed by hand: they do not come back when the playlist is rebuilt, unless restored here
            val excluded = detail?.excluded.orEmpty()
            if (excluded.isNotEmpty()) item(key = "excluded-header") {
                TextButton(onClick = { showExcluded = !showExcluded }, modifier = Modifier.padding(horizontal = 8.dp, vertical = 8.dp)) {
                    Icon(if (showExcluded) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore, null, tint = Muted)
                    Spacer(Modifier.width(6.dp))
                    Text("Quitadas a mano (${excluded.size})", color = Muted)
                }
            }
            if (showExcluded) items(excluded, key = { "x-" + it.trackId }) { song ->
                SongRow(song, vm, dimmed = true) {
                    IconButton(onClick = { vm.addTracks(name, listOf(song)) }) {
                        Icon(Icons.Filled.Restore, "Recuperar", tint = Accent)
                    }
                }
            }
            item { Spacer(Modifier.height(96.dp)) }
        }
    }

    if (confirmPublish) AlertDialog(
        onDismissRequest = { confirmPublish = false },
        title = { Text("Publicar en Spotify") },
        text = { Text("Se creará (o actualizará) la playlist privada '$name' en tu cuenta de Spotify con estas ${detail?.tracks?.size ?: 0} canciones.") },
        confirmButton = {
            TextButton(onClick = {
                confirmPublish = false
                vm.order("publish", JSONObject().put("name", name), "Publicar '$name' en Spotify")
            }) { Text("Publicar") }
        },
        dismissButton = { TextButton(onClick = { confirmPublish = false }) { Text("Cancelar") } },
    )
    if (confirmDelete) AlertDialog(
        onDismissRequest = { confirmDelete = false },
        title = { Text("Eliminar '$name'") },
        text = { Text(if (detail?.summary?.source == "spotify")
                          "Es una playlist bajada de tu Spotify: deja de descargarse y sus canciones se quitan en la próxima " +
                          "limpieza si no están en tu top, favoritas u otra playlist. En Spotify no se toca."
                      else "Se borra del Mac (queda en Playlists/.papelera por si acaso) y, si es de ejemplo, no se vuelve a crear. " +
                      "No se toca la copia de Spotify. Si el Mac no está accesible, se borra al sincronizar.") },
        confirmButton = {
            TextButton(onClick = { confirmDelete = false; vm.deletePlaylist(name, onBack) }) { Text("Eliminar", color = Danger) }
        },
        dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Cancelar") } },
    )
    if (showAdd) AddSongsSheet(name, vm, present = detail?.tracks?.map { it.trackId }?.toSet() ?: emptySet()) { showAdd = false }
}

@Composable
private fun Header(name: String, ids: List<String>, vm: AppViewModel, subtitle: String, note: String?) {
    Box(Modifier.fillMaxWidth().background(Brush.verticalGradient(listOf(colorFor(name).copy(alpha = 0.45f), Color.Transparent)))) {
        Column(Modifier.fillMaxWidth().padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            CoverMosaic(ids, name, { vm.coverUrl(it, 300) }, Modifier.width(200.dp))
            Spacer(Modifier.height(12.dp))
            Text(name, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(subtitle, color = Muted, style = MaterialTheme.typography.bodyMedium)
            note?.let { Text(it, color = Warning, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 4.dp)) }
        }
    }
}

@Composable
fun SongRow(song: Song, vm: AppViewModel, dimmed: Boolean = false, trailing: @Composable () -> Unit) {
    Row(Modifier.fillMaxWidth().alpha(if (dimmed) 0.55f else 1f).padding(horizontal = 16.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically) {
        CoverImage(vm.coverUrl(song.trackId, 120), song.artist, Modifier.size(52.dp), RoundedCornerShape(6.dp))
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(song.title, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(song.artist, color = Muted, style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
            val details = listOfNotNull(
                song.genre,
                song.bpm?.let { "${it.toInt()} BPM" },
                song.energy?.let { "energía ${it.toInt()}" },
            ).joinToString(" · ")
            if (details.isNotEmpty()) Text(details, color = Muted.copy(alpha = 0.7f), style = MaterialTheme.typography.labelSmall,
                                           maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        trailing()
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddSongsSheet(name: String, vm: AppViewModel, present: Set<String>, onDismiss: () -> Unit) {
    var query by remember { mutableStateOf("") }
    var results by remember { mutableStateOf<List<Song>>(emptyList()) }
    var added by remember { mutableStateOf(setOf<String>()) }

    LaunchedEffect(query) {
        delay(350) // waits until you finish typing
        results = vm.searchLibrary(query)
    }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.padding(horizontal = 16.dp)) {
            Text("Añadir a '$name'", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(query, { query = it }, Modifier.fillMaxWidth(), singleLine = true,
                              leadingIcon = { Icon(Icons.Filled.Search, null) }, placeholder = { Text("Artista o canción") })
            Text("Canciones de tu biblioteca. Se quedan en la playlist aunque no cumplan sus condiciones.", color = Muted,
                 style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 6.dp))
            HorizontalDivider(Modifier.padding(top = 8.dp))
        }
        LazyColumn(Modifier.fillMaxWidth().height(460.dp)) {
            items(results, key = { it.trackId }) { song ->
                SongRow(song, vm) {
                    val already = song.trackId in present || song.trackId in added
                    IconButton(enabled = !already, onClick = {
                        vm.addTracks(name, listOf(song))
                        added = added + song.trackId
                    }) { Icon(Icons.Filled.Add, "Añadir", tint = if (already) Muted.copy(alpha = 0.3f) else Accent) }
                }
            }
        }
    }
}
