package com.mimusica.app

import android.Manifest
import android.os.Build
import android.content.Intent
import android.os.Bundle
import androidx.compose.runtime.mutableStateOf
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.IconButton
import androidx.compose.ui.unit.dp
import com.mimusica.app.ui.Connection
import com.mimusica.app.ui.ProfileAvatar
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.AddCircle
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.LibraryMusic
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.mimusica.app.ui.AppViewModel
import com.mimusica.app.ui.CreateScreen
import com.mimusica.app.ui.HomeScreen
import com.mimusica.app.ui.PlaylistDetailScreen
import com.mimusica.app.ui.PlaylistsScreen
import com.mimusica.app.ui.SettingsScreen
import com.mimusica.app.ui.theme.MiMusicaTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (Build.VERSION.SDK_INT >= 33) {
            registerForActivityResult(ActivityResultContracts.RequestPermission()) {}
                .launch(Manifest.permission.POST_NOTIFICATIONS)
        }
        // A notification may ask to open a screen; only on a fresh start (not after a rotation)
        if (savedInstanceState == null) openAt.value = consumeOpenExtra(intent)
        setContent { MiMusicaTheme { AppRoot(openAt = openAt.value, onOpened = { openAt.value = null }) } }
    }

    override fun onStart() { super.onStart(); visible = true }
    override fun onStop() { visible = false; super.onStop() }

    /**
     * With the app already open (singleTop), the notification arrives here instead of opening another copy.
     */
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        consumeOpenExtra(intent)?.let { openAt.value = it }
    }

    private val openAt = mutableStateOf<String?>(null)

    private fun consumeOpenExtra(intent: Intent?): String? =
        intent?.getStringExtra(EXTRA_OPEN)?.also { intent.removeExtra(EXTRA_OPEN) }

    companion object {
        /** Screen a notification asks to open (e.g. "reviews"). */
        const val EXTRA_OPEN = "open"

        /** The app is on screen: what it shows counts as seen (no need to notify). */
        @Volatile var visible = false
    }
}

private data class Tab(val route: String, val label: String, val icon: androidx.compose.ui.graphics.vector.ImageVector)

// Settings is not a tab: it opens from the profile circle in the top bar (like in Spotify)
private val TABS = listOf(
    Tab("home", "Inicio", Icons.Filled.Home),
    Tab("playlists", "Playlists", Icons.Filled.LibraryMusic),
    Tab("create", "Crear", Icons.Filled.AddCircle),
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AppRoot(openAt: String? = null, onOpened: () -> Unit = {}, vm: AppViewModel = viewModel()) {
    val state by vm.state.collectAsStateWithLifecycle()
    val nav = rememberNavController()
    val backStack by nav.currentBackStackEntryAsState()
    val route = backStack?.destination?.route
    val snackbar = remember { SnackbarHostState() }

    LaunchedEffect(openAt) {
        if (openAt == "reviews") nav.navigate("reviews") { launchSingleTop = true }
        if (openAt != null) onOpened()
    }

    LaunchedEffect(state.message) {
        state.message?.let { snackbar.showSnackbar(it); vm.say(null) }
    }

    fun go(r: String) = nav.navigate(r) {
        popUpTo(nav.graph.findStartDestination().id) { saveState = true }
        launchSingleTop = true
        restoreState = true
    }

    Scaffold(
        topBar = {
            when (route) {
                "playlist/{name}" -> Unit
                "settings", "reviews", "spotify" -> TopAppBar(
                    title = { Text(when (route) { "settings" -> "Ajustes"; "spotify" -> "Playlists de Spotify"; else -> "Versiones para revisar" },
                                   fontWeight = FontWeight.Bold) },
                    navigationIcon = {
                        IconButton(onClick = { nav.popBackStack() }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Volver") }
                    },
                    colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
                )
                else -> TopAppBar(
                    navigationIcon = {
                        ProfileAvatar(vm.macName, state.connection, 34.dp,
                            Modifier.padding(start = 12.dp, end = 4.dp), onClickLabel = "Ajustes") {
                            nav.navigate("settings") { launchSingleTop = true }
                        }
                    },
                    title = { Text(TABS.firstOrNull { it.route == route }?.label?.let { if (it == "Inicio") "Mi Música" else it } ?: "Mi Música",
                                   fontWeight = FontWeight.Bold) },
                    actions = {
                        // Sync with the Mac (or look for it if not connected)
                        if (state.connection == Connection.SEARCHING || state.syncing) {
                            CircularProgressIndicator(Modifier.padding(end = 16.dp).size(20.dp), strokeWidth = 2.dp)
                        } else IconButton(onClick = { vm.syncNow() }) {
                            Icon(Icons.Filled.Sync, "Sincronizar con el Mac")
                        }
                    },
                    colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
                )
            }
        },
        bottomBar = {
            if (route != "settings" && route != "reviews" && route != "spotify") NavigationBar {
                TABS.forEach { tab ->
                    NavigationBarItem(
                        selected = route == tab.route || (tab.route == "playlists" && route == "playlist/{name}"),
                        onClick = { go(tab.route) },
                        icon = { Icon(tab.icon, null) },
                        label = { Text(tab.label) },
                    )
                }
            }
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        NavHost(nav, startDestination = "home", modifier = Modifier.padding(padding)) {
            composable("home") {
                HomeScreen(state, vm, onOpenSettings = { nav.navigate("settings") { launchSingleTop = true } },
                           onOpenReviews = { nav.navigate("reviews") { launchSingleTop = true } })
            }
            composable("reviews") { com.mimusica.app.ui.ReviewsScreen(state, vm) }
            composable("playlists") {
                LaunchedEffect(Unit) { vm.refreshAll() }
                PlaylistsScreen(state, vm, onOpenSpotify = { nav.navigate("spotify") { launchSingleTop = true } }) { name ->
                    nav.navigate("playlist/${android.net.Uri.encode(name)}")
                }
            }
            composable("spotify") { com.mimusica.app.ui.SpotifyPlaylistsScreen(state, vm) }
            composable("playlist/{name}") { entry ->
                // Navigation already decodes the argument (decoding again breaks names with "%")
                val name = entry.arguments?.getString("name").orEmpty()
                PlaylistDetailScreen(name, state, vm, onBack = { nav.popBackStack() })
            }
            composable("create") { CreateScreen(state, vm, onCreated = { go("home") }) }
            composable("settings") { SettingsScreen(state, vm) }
        }
    }
}
