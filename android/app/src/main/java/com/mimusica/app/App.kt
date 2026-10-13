package com.mimusica.app

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import coil3.ImageLoader
import coil3.PlatformContext
import coil3.SingletonImageLoader
import coil3.disk.DiskCache
import coil3.disk.directory
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import coil3.request.crossfade
import com.mimusica.app.data.AgentApi
import com.mimusica.app.work.SyncWorker

class App : Application(), SingletonImageLoader.Factory {

    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL_ORDERS, "Órdenes al Mac", NotificationManager.IMPORTANCE_DEFAULT)
                .apply { description = "Aviso cuando el Mac termina una orden" }
        )
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL_REVIEWS, "Versiones para revisar", NotificationManager.IMPORTANCE_DEFAULT)
                .apply { description = "Al terminar una orden del móvil, si se ha descargado una versión distinta de la de Spotify" }
        )
        // On app start, try to deliver anything pending
        SyncWorker.schedule(this)
    }

    /** Covers: disk cache so they are not requested from the Mac every time. */
    override fun newImageLoader(context: PlatformContext): ImageLoader =
        ImageLoader.Builder(context)
            .components { add(OkHttpNetworkFetcherFactory(callFactory = { AgentApi.http })) }
            .diskCache { DiskCache.Builder().directory(cacheDir.resolve("covers")).maxSizeBytes(150L * 1024 * 1024).build() }
            .crossfade(true)
            .build()

    companion object {
        const val CHANNEL_ORDERS = "orders"
        const val CHANNEL_REVIEWS = "reviews"
    }
}
