package org.nexusai.mobile

import android.app.DownloadManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Environment
import android.provider.Settings
import androidx.core.content.FileProvider
import java.io.File

class UpdateInstaller(private val context: Context) {
    fun canInstallUpdates(): Boolean = context.packageManager.canRequestPackageInstalls()

    fun requestInstallPermission() {
        context.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES).apply {
            data = Uri.parse("package:${context.packageName}")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        })
    }

    fun downloadAndPrompt(releaseUrl: String) {
        if (!canInstallUpdates()) {
            requestInstallPermission()
            return
        }
        val destination = File(
            context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS),
            "nexusai-update.apk",
        )
        destination.parentFile?.mkdirs()
        destination.delete()

        val request = DownloadManager.Request(Uri.parse(releaseUrl))
            .setTitle("NexusAI update")
            .setDescription("Downloading the latest NexusAI Android app")
            .setMimeType("application/vnd.android.package-archive")
            .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
            .setDestinationInExternalFilesDir(context, Environment.DIRECTORY_DOWNLOADS, destination.name)
        val downloadManager = context.getSystemService(DownloadManager::class.java)
        val downloadId = downloadManager.enqueue(request)

        Thread {
            while (true) {
                val cursor = downloadManager.query(DownloadManager.Query().setFilterById(downloadId))
                val status = cursor.use {
                    if (!it.moveToFirst()) DownloadManager.STATUS_FAILED
                    else it.getInt(it.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS))
                }
                if (status == DownloadManager.STATUS_SUCCESSFUL) {
                    val apkUri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", destination)
                    val installIntent = Intent(Intent.ACTION_VIEW).apply {
                        setDataAndType(apkUri, "application/vnd.android.package-archive")
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                    }
                    context.startActivity(installIntent)
                    return@Thread
                }
                if (status == DownloadManager.STATUS_FAILED) return@Thread
                Thread.sleep(500)
            }
        }.start()
    }
}
