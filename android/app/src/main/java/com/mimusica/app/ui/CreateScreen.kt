package com.mimusica.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.PlaylistAdd
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.InputChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RangeSlider
import androidx.compose.material3.Slider
import androidx.compose.material3.SuggestionChip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.mimusica.app.data.Meta
import com.mimusica.app.ui.theme.Muted
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.roundToInt

/** Playlists from your MP3s (what you listen to and downloaded), filtered by conditions. */
@Composable
fun CreateScreen(state: UiState, vm: AppViewModel, onCreated: () -> Unit) {
    val meta = state.meta
    if (meta == null) {
        Text("Conecta con el Mac una vez para cargar tus géneros y artistas (después se guardan en el móvil y " +
             "puedes crear playlists sin conexión).", color = Muted, modifier = Modifier.padding(16.dp))
    } else ConditionsForm(meta, state.playlists.map { it.name }, vm, onCreated)
}

/**
 * Like playlist_store.safe_name / file_key on the Mac: macOS disks are case-insensitive. Python uses
 * casefold(); uppercasing then lowercasing gives the same result in the usual cases ("ß" -> "ss").
 */
private fun fileKey(name: String) =
    name.map { if (it.isLetterOrDigit() || it in " -_+()") it else '_' }.joinToString("").trim()
        .uppercase(java.util.Locale.ROOT).lowercase(java.util.Locale.ROOT)

/**
 * The same checks the Mac does when it receives the job (playlist_store.name_problem), with what the
 * phone knows: so, offline, you are told right away instead of failing at sync time.
 */
private fun nameProblem(name: String, existing: List<String>, presets: List<String>): String? {
    val n = name.trim()
    if (n.isEmpty()) return null   // the button is already disabled
    if (fileKey(n).trim('_', ' ').isEmpty()) return "El nombre necesita alguna letra o número"
    if (n.any { it in "/%\\" }) return "El nombre no puede llevar / % ni \\"
    val key = fileKey(n)
    presets.firstOrNull { fileKey(it) == key }?.let { return "'$it' es una de las playlists de ejemplo: elige otro nombre" }
    existing.firstOrNull { fileKey(it) == key }?.let { return "Ya hay una playlist con ese nombre o muy parecido ('$it')" }
    return null
}

private val METRIC_LABELS = linkedMapOf(
    "energy" to "Energía", "danceability" to "Bailable", "valence" to "Alegría (valence)",
    "acousticness" to "Acústica", "speechiness" to "Rap / hablado", "bpm_exact" to "BPM",
)
private val GROUP_LABELS = linkedMapOf(
    "energy_group" to "Energía", "danceability_group" to "Bailable", "valence_group" to "Ánimo", "mode" to "Modo",
)
private val ORDER_LABELS = linkedMapOf(
    "" to "Más afines", "bpm_exact" to "BPM ↑", "-bpm_exact" to "BPM ↓", "-energy" to "Más energía",
    "energy" to "Menos energía", "-valence" to "Más alegres", "valence" to "Más tristes",
    "-acousticness" to "Más acústicas", "camelot" to "Tonalidad (para mezclar)",
)

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ConditionsForm(meta: Meta, existing: List<String>, vm: AppViewModel, onCreated: () -> Unit) {
    var name by rememberSaveable { mutableStateOf("") }
    val genres = remember { mutableStateMapOf<String, Boolean>() }
    val artists = remember { mutableStateMapOf<String, Boolean>() }
    val groups = remember { mutableStateMapOf<String, Set<String>>() }
    val ranges = remember { mutableStateMapOf<String, ClosedFloatingPointRange<Float>>() }
    var artistQuery by remember { mutableStateOf("") }
    var limit by rememberSaveable { mutableStateOf(40f) }
    var orderBy by rememberSaveable { mutableStateOf("") }

    Column(Modifier.verticalScroll(rememberScrollState()).padding(16.dp)) {
        Text("Se rellena con tus canciones descargadas y se actualiza sola con las nuevas. Lo que quites o añadas a mano se respeta.",
             color = Muted, style = MaterialTheme.typography.bodyMedium)
        Spacer(Modifier.height(12.dp))
        val problem = nameProblem(name, existing, meta.presets)
        OutlinedTextField(name, { name = it }, Modifier.fillMaxWidth(), label = { Text("Nombre de la playlist") }, singleLine = true,
                          isError = problem != null, supportingText = problem?.let { { Text(it) } })

        SectionTitle("Géneros")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            meta.genres.forEach { (g, count) ->
                FilterChip(genres[g] == true, { genres[g] = genres[g] != true }, { Text("$g ($count)") })
            }
        }

        SectionTitle("Artistas")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            artists.filterValues { it }.keys.forEach { a ->
                InputChip(true, { artists.remove(a) }, { Text(a) },
                          trailingIcon = { Icon(Icons.Filled.Close, null, Modifier.size(16.dp)) })
            }
        }
        OutlinedTextField(artistQuery, { artistQuery = it }, Modifier.fillMaxWidth(), singleLine = true,
                          placeholder = { Text("Buscar artista…") })
        if (artistQuery.length >= 2) FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            meta.artists.filter { it.first.contains(artistQuery, ignoreCase = true) }.take(8).forEach { (a, count) ->
                SuggestionChip({ artists[a] = true; artistQuery = "" }, { Text("$a ($count)") })
            }
        }

        SectionTitle("Ánimo y ritmo")
        GROUP_LABELS.forEach { (key, label) ->
            val options = meta.groups[key].orEmpty().filter { it != "Unknown" }
            if (options.isEmpty()) return@forEach
            Text(label, color = Muted, style = MaterialTheme.typography.labelLarge)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                options.forEach { opt ->
                    val selected = opt in groups[key].orEmpty()
                    FilterChip(selected, {
                        groups[key] = if (selected) groups[key].orEmpty() - opt else groups[key].orEmpty() + opt
                    }, { Text(opt) })
                }
            }
        }

        SectionTitle("Valores (0-100)")
        METRIC_LABELS.forEach { (key, label) ->
            val full = meta.metrics[key] ?: return@forEach
            val range = ranges[key] ?: full
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(label, Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                Text("${range.start.roundToInt()} – ${range.endInclusive.roundToInt()}", color = Muted,
                     style = MaterialTheme.typography.bodySmall)
            }
            RangeSlider(value = range, onValueChange = { ranges[key] = it }, valueRange = full)
        }

        SectionTitle("Número de canciones: ${limit.roundToInt()}")
        Slider(limit, { limit = it }, valueRange = 10f..100f, steps = 17)

        SectionTitle("Orden")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            ORDER_LABELS.filterKeys { it in meta.orderBy || it.isEmpty() }.forEach { (key, label) ->
                FilterChip(orderBy == key, { orderBy = key }, { Text(label) })
            }
        }

        Spacer(Modifier.height(16.dp))
        Button(
            onClick = {
                val criteria = JSONObject()
                genres.filterValues { it }.keys.takeIf { it.isNotEmpty() }?.let { criteria.put("genre", JSONArray(it.toList())) }
                artists.filterValues { it }.keys.takeIf { it.isNotEmpty() }?.let { criteria.put("artist_name", JSONArray(it.toList())) }
                groups.forEach { (k, v) -> if (v.isNotEmpty()) criteria.put(k, JSONArray(v.toList())) }
                ranges.forEach { (k, r) ->
                    val full = meta.metrics[k] ?: return@forEach
                    if (r.start > full.start + 0.5f) criteria.put("${k}_min", r.start.roundToInt())
                    if (r.endInclusive < full.endInclusive - 0.5f) criteria.put("${k}_max", r.endInclusive.roundToInt())
                }
                val params = JSONObject().put("name", name.trim()).put("criteria", criteria)
                    .put("limit", limit.roundToInt()).put("order_by", orderBy)
                vm.order("build_playlist", params, "Crear '${name.trim()}'")
                onCreated()
            },
            enabled = name.isNotBlank() && problem == null,
            modifier = Modifier.fillMaxWidth().height(52.dp),
        ) {
            Icon(Icons.Filled.PlaylistAdd, null)
            Spacer(Modifier.size(8.dp))
            Text("Crear playlist", fontWeight = FontWeight.SemiBold)
        }
        Spacer(Modifier.height(32.dp))
    }
}
