package com.mimusica.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.ExpandLess
import androidx.compose.material.icons.filled.ExpandMore
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.LinkOff
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.mimusica.app.ui.theme.AccentDark
import com.mimusica.app.ui.theme.Danger
import com.mimusica.app.ui.theme.Muted

/** Settings: opens from the profile circle. Header with the Mac and sections in a list. */
@Composable
fun SettingsScreen(state: UiState, vm: AppViewModel) {
    var code by remember { mutableStateOf("") }
    var manualIp by remember { mutableStateOf(state.macAddress ?: "") }
    var showManual by remember { mutableStateOf(false) }

    Column(Modifier.verticalScroll(rememberScrollState()).padding(horizontal = 16.dp),
           verticalArrangement = Arrangement.spacedBy(12.dp)) {
        ProfileHeader(state, vm)

        if (state.connection != Connection.ONLINE) Card(shape = RoundedCornerShape(14.dp)) {
            Column(Modifier.padding(16.dp)) {
                Text("Emparejar este móvil", fontWeight = FontWeight.Bold, style = MaterialTheme.typography.titleMedium)
                Text("Solo hace falta una vez. El Mac mostrará un código de 6 cifras en una notificación.", color = Muted)
                Spacer(Modifier.height(8.dp))
                if (state.pairingId == null) {
                    Button(onClick = { vm.startPairing() }, enabled = state.connection == Connection.NEEDS_PAIRING) {
                        Text("Pedir código al Mac")
                    }
                } else {
                    OutlinedTextField(code, { code = it.filter { c -> c in '0'..'9' }.take(6) }, singleLine = true,
                                      label = { Text("Código") },
                                      keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 8.dp)) {
                        Button(onClick = { vm.finishPairing(code) }, enabled = code.length == 6) { Text("Emparejar") }
                        TextButton(onClick = { vm.startPairing() }) { Text("Otro código") }
                    }
                }
            }
        }

        SectionLabel("Conexión")
        Group {
            SettingRow(Icons.Filled.Refresh, "Buscar el Mac de nuevo", "En la WiFi de casa") { vm.connect() }
            HorizontalDivider(color = Color.White.copy(alpha = 0.06f))
            SettingRow(Icons.Filled.Edit, "Dirección manual",
                       "Solo si no lo encuentra solo" + (state.macAddress?.let { " · ahora $it" } ?: ""),
                       trailing = if (showManual) Icons.Filled.ExpandLess else Icons.Filled.ExpandMore) { showManual = !showManual }
            if (showManual) Column(Modifier.padding(start = 16.dp, end = 16.dp, bottom = 12.dp)) {
                OutlinedTextField(manualIp, { manualIp = it }, singleLine = true, modifier = Modifier.fillMaxWidth(),
                                  placeholder = { Text("192.168.1.146") },
                                  keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
                TextButton(onClick = { vm.setManualAddress(manualIp) }) { Text("Usar esta dirección") }
            }
        }

        SectionLabel("Acerca de")
        Group {
            SettingRow(Icons.Filled.Info, "Mi Música ${com.mimusica.app.BuildConfig.VERSION_NAME}",
                       state.status?.let { "${it.songs} canciones en el Mac" } ?: "App del móvil")
            if (state.connection == Connection.ONLINE) {
                HorizontalDivider(color = Color.White.copy(alpha = 0.06f))
                SettingRow(Icons.Filled.LinkOff, "Olvidar emparejado", "Habrá que pedir otro código al Mac",
                           tint = Danger) { vm.forgetPairing() }
            }
        }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun ProfileHeader(state: UiState, vm: AppViewModel) {
    val status = when (state.connection) {
        Connection.SEARCHING -> "Buscando en la WiFi…"
        Connection.NOT_FOUND -> "No encontrado. ¿Está encendido y en la misma WiFi?"
        Connection.NEEDS_PAIRING -> "Encontrado en ${state.macAddress}, sin emparejar"
        Connection.ONLINE -> "Conectado · ${state.macAddress}"
    }
    Box(Modifier.fillMaxWidth().padding(top = 8.dp)
            .background(Brush.verticalGradient(listOf(AccentDark.copy(alpha = 0.35f), Color.Transparent)), RoundedCornerShape(20.dp))
            .padding(20.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            ProfileAvatar(vm.macName, state.connection, 64.dp)
            Spacer(Modifier.width(16.dp))
            Column {
                Text(vm.macName?.substringBefore('.') ?: "Mi Mac", fontWeight = FontWeight.Bold,
                     style = MaterialTheme.typography.titleLarge)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Pill(when (state.connection) {
                        Connection.ONLINE -> "En línea"
                        Connection.SEARCHING -> "Buscando"
                        Connection.NEEDS_PAIRING -> "Sin emparejar"
                        Connection.NOT_FOUND -> "Sin conexión"
                    }, connectionColor(state.connection))
                }
                Text(status, color = Muted, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(top = 4.dp))
            }
        }
    }
}

@Composable
private fun SectionLabel(text: String) {
    Text(text.uppercase(), color = Muted, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold,
         modifier = Modifier.padding(start = 4.dp, top = 8.dp))
}

@Composable
private fun Group(content: @Composable () -> Unit) {
    Card(shape = RoundedCornerShape(14.dp), colors = CardDefaults.cardColors()) { Column { content() } }
}

@Composable
private fun SettingRow(icon: ImageVector, title: String, subtitle: String? = null, tint: Color? = null,
                       trailing: ImageVector? = null, onClick: (() -> Unit)? = null) {
    Row(
        Modifier.fillMaxWidth().let { if (onClick != null) it.clickable(onClick = onClick) else it }
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(icon, null, tint = tint ?: Muted, modifier = Modifier.size(24.dp))
        Spacer(Modifier.width(16.dp))
        Column(Modifier.weight(1f)) {
            Text(title, color = tint ?: MaterialTheme.colorScheme.onSurface, fontWeight = FontWeight.Medium)
            subtitle?.let { Text(it, color = Muted, style = MaterialTheme.typography.bodySmall) }
        }
        trailing?.let { Icon(it, null, tint = Muted) }
    }
}
