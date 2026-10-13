package com.mimusica.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MusicNote
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil3.compose.SubcomposeAsyncImage
import com.mimusica.app.ui.theme.AccentDark
import com.mimusica.app.ui.theme.SurfaceHigh

/** Stable colour per name, for cover backgrounds that have not loaded yet. */
fun colorFor(seed: String): Color {
    val palette = listOf(0xFF7C3AED, 0xFF2563EB, 0xFF0891B2, 0xFF059669, 0xFFD97706, 0xFFDB2777, 0xFF4F46E5)
    return Color(palette[(seed.hashCode() and 0x7fffffff) % palette.size])
}

@Composable
fun CoverImage(url: String?, seed: String, modifier: Modifier = Modifier, shape: Shape = RoundedCornerShape(8.dp)) {
    val placeholder = @Composable {
        Box(
            Modifier.fillMaxSize().background(Brush.linearGradient(listOf(colorFor(seed), AccentDark))),
            contentAlignment = Alignment.Center,
        ) { Icon(Icons.Filled.MusicNote, null, tint = Color.White.copy(alpha = 0.8f)) }
    }
    Box(modifier.clip(shape).background(SurfaceHigh)) {
        if (url == null) placeholder() else SubcomposeAsyncImage(
            model = url, contentDescription = null, contentScale = ContentScale.Crop,
            modifier = Modifier.fillMaxSize(),
            loading = { placeholder() }, error = { placeholder() },
        )
    }
}

/** Playlist cover: 2x2 mosaic of the first songs (or a single one if there are fewer than 4). */
@Composable
fun CoverMosaic(ids: List<String>, name: String, coverUrl: (String) -> String?, modifier: Modifier = Modifier) {
    Box(modifier.aspectRatio(1f).clip(RoundedCornerShape(12.dp))) {
        if (ids.size < 4) {
            CoverImage(ids.firstOrNull()?.let(coverUrl), name, Modifier.fillMaxSize(), RoundedCornerShape(0.dp))
        } else {
            Column(Modifier.fillMaxSize()) {
                for (row in 0..1) Row(Modifier.weight(1f)) {
                    for (col in 0..1) CoverImage(
                        coverUrl(ids[row * 2 + col]), name + row + col,
                        Modifier.weight(1f).fillMaxSize(), RoundedCornerShape(0.dp),
                    )
                }
            }
        }
    }
}

@Composable
fun Pill(text: String, color: Color, modifier: Modifier = Modifier) {
    Surface(color = color.copy(alpha = 0.18f), shape = RoundedCornerShape(50), modifier = modifier) {
        Text(text, color = color, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
             modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp))
    }
}

@Composable
fun SectionTitle(text: String, modifier: Modifier = Modifier) {
    Text(text, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold,
         modifier = modifier.fillMaxWidth().padding(top = 16.dp, bottom = 8.dp))
}


/** Initial for the profile circle: the Mac name's ("My-MacBook-Pro" -> "M"). */
fun initialOf(name: String?): String =
    name?.firstOrNull { it.isLetterOrDigit() }?.uppercaseChar()?.toString() ?: "M"

fun connectionColor(c: Connection): Color = when (c) {
    Connection.ONLINE -> com.mimusica.app.ui.theme.Accent
    Connection.SEARCHING, Connection.NEEDS_PAIRING -> com.mimusica.app.ui.theme.Warning
    Connection.NOT_FOUND -> com.mimusica.app.ui.theme.Danger
}

/** Profile circle (like Spotify's) with a dot in the colour of the connection state. */
@Composable
fun ProfileAvatar(name: String?, connection: Connection, size: Dp, modifier: Modifier = Modifier) {
    Box(modifier.size(size)) {
        Box(
            Modifier.fillMaxSize().clip(androidx.compose.foundation.shape.CircleShape)
                .background(Brush.linearGradient(listOf(com.mimusica.app.ui.theme.Accent, AccentDark))),
            contentAlignment = Alignment.Center,
        ) {
            Text(initialOf(name), color = Color(0xFF052E16), fontWeight = FontWeight.Bold,
                 fontSize = (size.value * 0.45f).sp)
        }
        Box(
            Modifier.align(Alignment.BottomEnd).size(size * 0.3f)
                .clip(androidx.compose.foundation.shape.CircleShape)
                .background(com.mimusica.app.ui.theme.Background).padding(size * 0.05f)
                .clip(androidx.compose.foundation.shape.CircleShape).background(connectionColor(connection)),
        )
    }
}
