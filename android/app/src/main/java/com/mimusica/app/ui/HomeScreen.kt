package com.mimusica.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CloudDownload
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Hearing
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mimusica.app.data.Order
import com.mimusica.app.ui.theme.Accent
import com.mimusica.app.ui.theme.AccentDark
import com.mimusica.app.ui.theme.Danger
import com.mimusica.app.ui.theme.Muted
import com.mimusica.app.ui.theme.Warning
import org.json.JSONObject

@Composable
fun HomeScreen(state: UiState, vm: AppViewModel, onOpenSettings: () -> Unit, onOpenReviews: () -> Unit = {}) {
    LazyColumn(Modifier.padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item { Spacer(Modifier.height(8.dp)) }
        item { ConnectionCard(state, vm, onOpenSettings) }
        if (state.reviews.isNotEmpty()) item {
            Card(onClick = onOpenReviews, shape = RoundedCornerShape(14.dp),
                 colors = CardDefaults.cardColors(containerColor = Warning.copy(alpha = 0.14f))) {
                Row(Modifier.fillMaxWidth().padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Filled.Hearing, null, tint = Warning)
                    Spacer(Modifier.width(12.dp))
                    Column(Modifier.weight(1f)) {
                        val n = state.reviews.size
                        Text(if (n == 1) "1 versión para revisar" else "$n versiones para revisar", fontWeight = FontWeight.SemiBold)
                        Text("No son la versión de Spotify: escúchalas y apruébalas o quítalas",
                             color = Muted, style = MaterialTheme.typography.bodySmall)
                    }
                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, null, tint = Muted)
                }
            }
        }
        item {
            SectionTitle("Acciones")
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                ActionTile("Actualizar biblioteca", "Descarga tu top y favoritas; quita lo que ya no escuchas", Icons.Filled.CloudDownload,
                    Modifier.weight(1f)) {
                    vm.order("refresh", JSONObject(), "Actualizar biblioteca")
                }
                ActionTile("Regenerar playlists", "Con las canciones actuales", Icons.Filled.Refresh,
                    Modifier.weight(1f)) {
                    vm.order("rebuild_playlists", JSONObject(), "Regenerar playlists")
                }
            }
        }
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                SectionTitle("Órdenes", Modifier.weight(1f))
                if (state.orders.any { !it.isOpen }) TextButton(onClick = vm::clearFinishedOrders) { Text("Limpiar") }
            }
        }
        if (state.orders.isEmpty()) item {
            Text("Todavía no hay órdenes. Las que hagas se guardan aquí y se envían al Mac en cuanto esté accesible.",
                 color = Muted, style = MaterialTheme.typography.bodyMedium)
        }
        // Playlist edits are only listed while waiting for the Mac or if they failed
        items(state.orders.filter { !it.isSilent || it.state != Order.DONE || it.cancelFailed }, key = { it.clientId }) { order ->
            OrderCard(order, state.liveLog[order.jobId]) { vm.cancelOrder(order) }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }
}

@Composable
private fun ConnectionCard(state: UiState, vm: AppViewModel, onOpenSettings: () -> Unit) {
    val (title, subtitle, color) = when (state.connection) {
        Connection.SEARCHING -> Triple("Buscando el Mac…", "En la WiFi de casa", Warning)
        Connection.NOT_FOUND -> Triple("Mac no accesible",
            "Las órdenes esperan en el móvil y se enviarán solas" +
                (state.savedAt?.let { "\nDatos guardados el ${it.replace('T', ' ').dropLast(3)}" } ?: ""), Danger)
        Connection.NEEDS_PAIRING -> Triple("Mac encontrado", "Falta emparejar este móvil", Warning)
        Connection.ONLINE -> Triple(vm.macName?.substringBefore('.') ?: "Mac conectado",
            state.status?.let { "${it.songs} canciones · ${it.playlists} playlists" } ?: "Conectado", Accent)
    }
    Card(
        shape = RoundedCornerShape(16.dp),
        colors = CardDefaults.cardColors(containerColor = Color.Transparent),
        onClick = { if (state.connection == Connection.ONLINE) vm.refreshAll() else onOpenSettings() },
    ) {
        Box(Modifier.fillMaxWidth().background(Brush.linearGradient(listOf(AccentDark.copy(alpha = 0.55f), Color(0xFF1E1B4B)))).padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(44.dp).clip(CircleShape).background(Color.White.copy(alpha = 0.12f)), contentAlignment = Alignment.Center) {
                    if (state.connection == Connection.SEARCHING) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp)
                    else Icon(Icons.Filled.Computer, null)
                }
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(Modifier.size(8.dp).clip(CircleShape).background(color))
                        Spacer(Modifier.width(6.dp))
                        Text(title, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.titleMedium)
                    }
                    Text(subtitle, color = Color.White.copy(alpha = 0.75f), style = MaterialTheme.typography.bodySmall)
                    state.status?.lastRefresh?.takeIf { state.connection == Connection.ONLINE }?.let {
                        Text("Última actualización: ${it.replace('T', ' ').dropLast(3)}",
                             color = Color.White.copy(alpha = 0.6f), style = MaterialTheme.typography.bodySmall)
                    }
                }
                if (state.connection == Connection.NOT_FOUND) TextButton(onClick = { vm.connect() }) { Text("Reintentar") }
            }
        }
    }
}

@Composable
private fun ActionTile(title: String, subtitle: String, icon: ImageVector, modifier: Modifier, onClick: () -> Unit) {
    Card(onClick = onClick, modifier = modifier, shape = RoundedCornerShape(14.dp)) {
        Column(Modifier.padding(14.dp)) {
            Icon(icon, null, tint = Accent)
            Spacer(Modifier.height(8.dp))
            Text(title, fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, color = Muted, style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun OrderCard(order: Order, live: List<String>?, onCancel: () -> Unit) {
    val (label, color) = when (order.state) {
        Order.PENDING -> "En espera" to Warning
        Order.SENT -> "En el Mac" to Color(0xFF93C5FD)
        Order.DONE -> "Hecho" to Accent
        Order.CANCELLED -> "Cancelada" to Muted
        Order.CANCELLING -> "Cancelando…" to Warning
        else -> "Error" to Danger
    }
    Card(shape = RoundedCornerShape(14.dp)) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(order.label, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f),
                     maxLines = 1, overflow = TextOverflow.Ellipsis)
                Pill(label, color)
            }
            Text(order.created.replace('T', ' ').dropLast(3), color = Muted, style = MaterialTheme.typography.bodySmall)
            order.message?.let { Text(it, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 4.dp)) }
            // Can be cancelled until done: on the phone, or on the Mac if still queued
            if (order.isOpen && order.state != Order.CANCELLING) TextButton(onClick = onCancel, contentPadding = PaddingValues(0.dp)) {
                Text(if (order.state == Order.PENDING) "Cancelar" else "Cancelar (si aún no ha empezado)")
            }
            if (order.state == Order.SENT && !live.isNullOrEmpty()) {
                Spacer(Modifier.height(6.dp))
                Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(Color.Black.copy(alpha = 0.35f)).padding(8.dp)) {
                    live.takeLast(4).forEach {
                        Text(it, fontFamily = FontFamily.Monospace, fontSize = 11.sp, color = Muted,
                             maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
            }
        }
    }
}
