package com.mimusica.app.data

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

/**
 * Finds the Mac on the Wi-Fi: first tries the last known address and, if it does not answer, looks
 * for the _spotmp3._tcp service the agent advertises (mDNS).
 */
class Discovery(private val context: Context, private val settings: Settings) {

    private val api = AgentApi(settings)

    /** true if the Mac answers (and saves its address in Settings). */
    suspend fun ensureReachable(): Boolean = withContext(Dispatchers.IO) {
        // Only accepted if the agent answers: the token is never sent to something else now at that address
        // (e.g. another device after the router changed IPs)
        if (settings.host != null && api.isAgent()) return@withContext true
        val found = discover() ?: return@withContext false
        settings.host = found.first
        settings.port = found.second
        api.isAgent()
    }

    /** (ip, port) of the first agent found on the Wi-Fi, or null after a few seconds. */
    suspend fun discover(timeoutMs: Long = 7000): Pair<String, Int>? {
        val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val lock = wifi.createMulticastLock("mimusica-discovery").apply { setReferenceCounted(false); acquire() }
        try {
            return withTimeoutOrNull(timeoutMs) {
                suspendCancellableCoroutine { cont ->
                    var finished = false
                    lateinit var listener: NsdManager.DiscoveryListener

                    fun finish(result: Pair<String, Int>?) {
                        synchronized(this@Discovery) {
                            if (finished) return
                            finished = true
                        }
                        runCatching { nsd.stopServiceDiscovery(listener) }
                        if (cont.isActive) cont.resume(result)
                    }

                    listener = object : NsdManager.DiscoveryListener {
                        override fun onServiceFound(info: NsdServiceInfo) {
                            if (!info.serviceType.contains("_spotmp3")) return
                            @Suppress("DEPRECATION")
                            nsd.resolveService(info, object : NsdManager.ResolveListener {
                                override fun onResolveFailed(i: NsdServiceInfo, error: Int) {}
                                override fun onServiceResolved(resolved: NsdServiceInfo) {
                                    @Suppress("DEPRECATION")
                                    val ip = resolved.host?.hostAddress ?: return
                                    finish(ip to resolved.port)
                                }
                            })
                        }
                        override fun onServiceLost(info: NsdServiceInfo) {}
                        override fun onDiscoveryStarted(serviceType: String) {}
                        override fun onDiscoveryStopped(serviceType: String) {}
                        override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) = finish(null)
                        override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
                    }
                    nsd.discoverServices("_spotmp3._tcp", NsdManager.PROTOCOL_DNS_SD, listener)
                    cont.invokeOnCancellation { runCatching { nsd.stopServiceDiscovery(listener) } }
                }
            }
        } finally {
            lock.release()
        }
    }
}
