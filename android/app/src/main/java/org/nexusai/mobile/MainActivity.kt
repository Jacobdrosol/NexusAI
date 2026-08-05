package org.nexusai.mobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = InstanceStore(applicationContext)
        setContent {
            MaterialTheme {
                NexusMobileApp(store)
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@androidx.compose.runtime.Composable
private fun NexusMobileApp(store: InstanceStore) {
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    var apiClient by remember { mutableStateOf(NexusApiClient(store)) }
    var session by remember { mutableStateOf<MobileSession?>(null) }
    var instanceUrl by remember { mutableStateOf(store.instanceUrl()?.toString().orEmpty()) }
    var status by remember { mutableStateOf("") }
    var loading by remember { mutableStateOf(false) }
    var restoringSession by remember { mutableStateOf(store.instanceUrl() != null) }
    var availableUpdate by remember { mutableStateOf<AndroidUpdate?>(null) }
    var updateRequired by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        if (store.instanceUrl() == null) return@LaunchedEffect
        val restored = withContext(Dispatchers.IO) { runCatching { apiClient.restoreSession() }.getOrNull() }
        session = restored
        restoringSession = false
        if (restored != null) {
            val update = withContext(Dispatchers.IO) { runCatching { apiClient.fetchBootstrap().androidUpdate }.getOrNull() }
            availableUpdate = update?.takeIf { it.latestVersionCode > BuildConfig.VERSION_CODE && it.releaseUrl.isNotBlank() }
            updateRequired = update?.minimumVersionCode?.let { it > BuildConfig.VERSION_CODE } == true
        }
    }

    Scaffold(
        topBar = { CenterAlignedTopAppBar(title = { Text("NexusAI") }) },
    ) { padding ->
        when {
            session != null -> HomeScreen(
                modifier = Modifier.padding(padding),
                session = session!!,
                update = availableUpdate,
                updateRequired = updateRequired,
                onInstallUpdate = { availableUpdate?.let { UpdateInstaller(store.context).downloadAndPrompt(it.releaseUrl) } },
                onDisconnect = {
                    apiClient.clearSession()
                    session = null
                    instanceUrl = ""
                    status = "Disconnected from this NexusAI instance."
                },
            )
            restoringSession -> RestoringSessionScreen(modifier = Modifier.padding(padding))
            store.instanceUrl() == null -> ConnectionScreen(
                modifier = Modifier.padding(padding),
                initialUrl = instanceUrl,
                status = status,
                loading = loading,
                onConnect = { rawUrl ->
                    val result = store.saveInstanceUrl(rawUrl)
                    result.onSuccess {
                        apiClient = NexusApiClient(store)
                        instanceUrl = it.toString()
                        status = "Instance saved. Sign in with your NexusAI account."
                    }.onFailure { error -> status = error.message.orEmpty() }
                },
            )
            else -> LoginScreen(
                modifier = Modifier.padding(padding),
                instanceUrl = instanceUrl,
                status = status,
                loading = loading,
                onSignIn = { email, password ->
                    loading = true
                    status = "Signing in…"
                    scope.launch {
                        runCatching {
                            withContext(Dispatchers.IO) { apiClient.signIn(email, password) }
                        }.onSuccess {
                            session = it
                            status = ""
                        }.onFailure { error ->
                            status = error.message ?: "Unable to sign in."
                        }
                        loading = false
                    }
                },
                onChangeInstance = {
                    apiClient.clearSession()
                    apiClient = NexusApiClient(store)
                    instanceUrl = ""
                    status = ""
                },
            )
        }
    }
}

@androidx.compose.runtime.Composable
private fun RestoringSessionScreen(modifier: Modifier) {
    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.Center,
    ) {
        Text("Restoring your NexusAI session…", style = MaterialTheme.typography.bodyLarge)
    }
}

@androidx.compose.runtime.Composable
private fun ConnectionScreen(
    modifier: Modifier,
    initialUrl: String,
    status: String,
    loading: Boolean,
    onConnect: (String) -> Unit,
) {
    var url by remember(initialUrl) { mutableStateOf(initialUrl) }
    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Connect your NexusAI instance", style = MaterialTheme.typography.headlineSmall)
        Text("Enter the HTTPS address for the NexusAI deployment you own or administer.")
        OutlinedTextField(
            value = url,
            onValueChange = { url = it },
            label = { Text("NexusAI URL") },
            placeholder = { Text("https://chat.example.com") },
            modifier = Modifier.fillMaxWidth(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            singleLine = true,
        )
        Button(enabled = !loading, onClick = { onConnect(url) }) { Text("Continue") }
        if (status.isNotBlank()) Text(status, color = MaterialTheme.colorScheme.error)
    }
}

@androidx.compose.runtime.Composable
private fun LoginScreen(
    modifier: Modifier,
    instanceUrl: String,
    status: String,
    loading: Boolean,
    onSignIn: (String, String) -> Unit,
    onChangeInstance: () -> Unit,
) {
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Sign in", style = MaterialTheme.typography.headlineSmall)
        Text(instanceUrl, style = MaterialTheme.typography.bodySmall)
        OutlinedTextField(value = email, onValueChange = { email = it }, label = { Text("Email") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(
            value = password,
            onValueChange = { password = it },
            label = { Text("Password") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
        )
        Button(enabled = !loading && email.isNotBlank() && password.isNotBlank(), onClick = { onSignIn(email, password) }) {
            Text(if (loading) "Signing in" else "Sign in")
        }
        Button(enabled = !loading, onClick = onChangeInstance) { Text("Change instance") }
        if (status.isNotBlank()) Text(status, color = MaterialTheme.colorScheme.error)
    }
}

@androidx.compose.runtime.Composable
private fun HomeScreen(
    modifier: Modifier,
    session: MobileSession,
    update: AndroidUpdate?,
    updateRequired: Boolean,
    onInstallUpdate: () -> Unit,
    onDisconnect: () -> Unit,
) {
    Column(
        modifier = modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Connected", style = MaterialTheme.typography.headlineSmall)
        Text(session.user.email)
        if (update != null) {
            Text(if (updateRequired) "An update is required to continue." else "A NexusAI update is available.")
            Button(onClick = onInstallUpdate) { Text("Install update") }
        }
        Text("Chat and Work monitoring are the next Android client screens.")
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(onClick = onDisconnect) { Text("Disconnect") }
        }
    }
}
