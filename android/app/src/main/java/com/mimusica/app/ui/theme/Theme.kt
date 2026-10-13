package com.mimusica.app.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

val Accent = Color(0xFF4ADE80)
val AccentDark = Color(0xFF15803D)
val Background = Color(0xFF0E0E11)
val Surface = Color(0xFF18181C)
val SurfaceHigh = Color(0xFF232329)
val Muted = Color(0xFFA1A1AA)
val Danger = Color(0xFFF87171)
val Warning = Color(0xFFFBBF24)

private val colors = darkColorScheme(
    primary = Accent,
    onPrimary = Color(0xFF052E16),
    primaryContainer = AccentDark,
    onPrimaryContainer = Color.White,
    secondary = Color(0xFF93C5FD),
    background = Background,
    onBackground = Color(0xFFF4F4F5),
    surface = Surface,
    onSurface = Color(0xFFF4F4F5),
    surfaceVariant = SurfaceHigh,
    onSurfaceVariant = Muted,
    surfaceContainer = Surface,
    surfaceContainerHigh = SurfaceHigh,
    error = Danger,
)

@Composable
fun MiMusicaTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = colors, typography = Typography(), content = content)
}
