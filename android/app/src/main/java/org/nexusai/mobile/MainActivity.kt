package org.nexusai.mobile

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
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
        setContent { MaterialTheme { NexusMobileApp(store) } }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun NexusMobileApp(store: InstanceStore) {
    val scope = rememberCoroutineScope()
    var apiClient by remember { mutableStateOf(NexusApiClient(store)) }
    var configuredInstance by remember { mutableStateOf(store.instanceUrl() != null) }
    var session by remember { mutableStateOf<MobileSession?>(null) }
    var status by remember { mutableStateOf("") }
    var loading by remember { mutableStateOf(false) }
    var restoringSession by remember { mutableStateOf(store.instanceUrl() != null) }
    var availableUpdate by remember { mutableStateOf<AndroidUpdate?>(null) }
    var updateRequired by remember { mutableStateOf(false) }
    var theme by remember { mutableStateOf(store.themePreference()) }

    val useDarkTheme = when (theme) {
        AppTheme.SYSTEM -> isSystemInDarkTheme()
        AppTheme.DARK -> true
        AppTheme.LIGHT -> false
    }

    MaterialTheme(colorScheme = if (useDarkTheme) darkColorScheme() else lightColorScheme()) {
    LaunchedEffect(Unit) {
        if (store.instanceUrl() == null) return@LaunchedEffect
        val restored = withContext(Dispatchers.IO) { runCatching { apiClient.restoreSession() }.getOrNull() }
        session = restored
        restoringSession = false
        if (restored != null) {
            val update = withContext(Dispatchers.IO) { runCatching { apiClient.fetchBootstrap().androidUpdate }.getOrNull() }
            availableUpdate = update?.takeIf { it.latestVersionCode > BuildConfig.VERSION_CODE && it.releaseUrl.isNotBlank() }
            updateRequired = update?.minimumVersionCode?.let { it > BuildConfig.VERSION_CODE } == true
            availableUpdate?.let { UpdateInstaller(store.context).downloadAndPrompt(it.releaseUrl) }
        }
    }

    Scaffold(topBar = { CenterAlignedTopAppBar(title = { Text("NexusAI") }) }) { padding ->
        when {
            restoringSession -> CenterMessage(Modifier.padding(padding), "Restoring your NexusAI session…")
            session != null -> ChatApp(
                modifier = Modifier.padding(padding),
                api = apiClient,
                update = availableUpdate,
                updateRequired = updateRequired,
                onInstallUpdate = { availableUpdate?.let { UpdateInstaller(store.context).downloadAndPrompt(it.releaseUrl) } },
                onSessionExpired = { session = null; status = "Your session expired. Sign in again." },
                onDisconnect = { apiClient.clearSession(); session = null; status = "Disconnected." },
                instanceUrl = store.instanceUrl()?.toString().orEmpty(),
                theme = theme,
                onThemeChange = { selected -> store.saveThemePreference(selected); theme = selected },
                onChangeInstance = { apiClient.clearSession(); apiClient = NexusApiClient(store); session = null; configuredInstance = false; status = "" },
            )
            !configuredInstance -> ConnectionScreen(Modifier.padding(padding)) { rawUrl ->
                store.saveInstanceUrl(rawUrl).onSuccess { apiClient = NexusApiClient(store); configuredInstance = true; status = "Instance saved. Sign in." }
                    .onFailure { status = it.message.orEmpty() }
            }
            else -> LoginScreen(
                modifier = Modifier.padding(padding),
                instanceUrl = store.instanceUrl().toString(),
                status = status,
                loading = loading,
                onSignIn = { email, password ->
                    loading = true
                    scope.launch {
                        runCatching { withContext(Dispatchers.IO) { apiClient.signIn(email, password) } }
                            .onSuccess {
                                session = it
                                status = ""
                                val update = withContext(Dispatchers.IO) { runCatching { apiClient.fetchBootstrap().androidUpdate }.getOrNull() }
                                availableUpdate = update?.takeIf { row -> row.latestVersionCode > BuildConfig.VERSION_CODE && row.releaseUrl.isNotBlank() }
                                updateRequired = update?.minimumVersionCode?.let { row -> row > BuildConfig.VERSION_CODE } == true
                                availableUpdate?.let { row -> UpdateInstaller(store.context).downloadAndPrompt(row.releaseUrl) }
                            }
                            .onFailure { status = it.message ?: "Unable to sign in." }
                        loading = false
                    }
                },
                onChangeInstance = { apiClient.clearSession(); apiClient = NexusApiClient(store); configuredInstance = false; status = "" },
            )
        }
    }
    }
}

@Composable
private fun ChatApp(
    modifier: Modifier,
    api: NexusApiClient,
    update: AndroidUpdate?,
    updateRequired: Boolean,
    onInstallUpdate: () -> Unit,
    onSessionExpired: () -> Unit,
    onDisconnect: () -> Unit,
    instanceUrl: String,
    theme: AppTheme,
    onThemeChange: (AppTheme) -> Unit,
    onChangeInstance: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    var projects by remember { mutableStateOf<List<ChatProject>>(emptyList()) }
    var conversations by remember { mutableStateOf<List<ChatConversation>>(emptyList()) }
    var selectedProject by remember { mutableStateOf<ChatProject?>(null) }
    var selectedConversation by remember { mutableStateOf<ChatConversation?>(null) }
    var messages by remember { mutableStateOf<List<ChatMessage>>(emptyList()) }
    var status by remember { mutableStateOf("") }
    var loading by remember { mutableStateOf(true) }
    var projectPickerOpen by remember { mutableStateOf(false) }
    var newChatOpen by remember { mutableStateOf(false) }
    var settingsOpen by remember { mutableStateOf(false) }
    var appSettingsOpen by remember { mutableStateOf(false) }
    var chatBootstrap by remember { mutableStateOf<ChatBootstrap?>(null) }

    fun loadMessages(conversation: ChatConversation) {
        scope.launch {
            runCatching { withContext(Dispatchers.IO) { api.listMessages(conversation.id) } }
                .onSuccess { messages = it }
                .onFailure { status = it.message ?: "Could not load messages."; if (it.message?.contains("session", true) == true) onSessionExpired() }
        }
    }
    fun loadConversations(selectId: String? = null) {
        loading = true
        scope.launch {
            runCatching { withContext(Dispatchers.IO) { api.listConversations(selectedProject?.id, selectedProject == null) } }
                .onSuccess { rows ->
                    conversations = rows
                    val next = rows.firstOrNull { it.id == selectId } ?: selectedConversation?.let { old -> rows.firstOrNull { it.id == old.id } }
                    selectedConversation = next
                    if (next != null) loadMessages(next)
                }
                .onFailure { status = it.message ?: "Could not load chats."; if (it.message?.contains("session", true) == true) onSessionExpired() }
            loading = false
        }
    }
    LaunchedEffect(Unit) {
        runCatching { withContext(Dispatchers.IO) { api.listProjects() } }.onSuccess { projects = it }
        loadConversations()
    }

    Column(modifier = modifier.fillMaxSize().padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        if (update != null) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(if (updateRequired) "Update required" else "Update available")
                Button(onClick = onInstallUpdate) { Text("Update") }
            }
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { projectPickerOpen = true }) {
                Text(selectedProject?.name ?: "Unscoped chats")
            }
            Button(onClick = { newChatOpen = true }) { Text("New chat") }
            Button(onClick = { appSettingsOpen = true }) { Text("App") }
            Button(onClick = onDisconnect) { Text("Sign out") }
        }
        if (selectedConversation == null) {
            Text("Chats", style = MaterialTheme.typography.titleMedium)
            if (loading) Text("Loading chats…")
            LazyColumn(Modifier.weight(1f)) {
                items(conversations) { conversation ->
                    Column(Modifier.fillMaxWidth().clickable { selectedConversation = conversation; loadMessages(conversation) }.padding(vertical = 14.dp)) {
                        Text(conversation.title, style = MaterialTheme.typography.titleSmall)
                        if (conversation.updatedAt.isNotBlank()) Text(conversation.updatedAt, style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        } else {
            ConversationScreen(
                modifier = Modifier.fillMaxWidth(),
                conversation = selectedConversation!!,
                messages = messages,
                loading = loading,
                status = status,
                onBack = { selectedConversation = null; messages = emptyList() },
                onRefresh = { loadMessages(selectedConversation!!) },
                onSettings = {
                    scope.launch {
                        runCatching { withContext(Dispatchers.IO) { api.chatBootstrap() } }
                            .onSuccess { chatBootstrap = it; settingsOpen = true }
                            .onFailure { status = it.message ?: "Chat settings are unavailable." }
                    }
                },
                onSend = { content ->
                    loading = true
                    scope.launch {
                        runCatching { withContext(Dispatchers.IO) { api.sendMessage(selectedConversation!!.id, content) } }
                            .onSuccess { loadMessages(selectedConversation!!) }
                            .onFailure { status = it.message ?: "Message failed."; if (it.message?.contains("session", true) == true) onSessionExpired() }
                        loading = false
                    }
                },
            )
        }
        if (status.isNotBlank()) Text(status, color = MaterialTheme.colorScheme.error)
    }
    if (projectPickerOpen) ProjectPicker(projects, selectedProject, onDismiss = { projectPickerOpen = false }) {
        selectedProject = it
        selectedConversation = null
        projectPickerOpen = false
        loadConversations()
    }
    if (newChatOpen) NewChatDialog(selectedProject, onDismiss = { newChatOpen = false }) { title ->
        newChatOpen = false
        loading = true
        scope.launch {
            runCatching { withContext(Dispatchers.IO) { api.createConversation(title, selectedProject?.id) } }
                .onSuccess { created -> loadConversations(created.id) }
                .onFailure { status = it.message ?: "Could not create chat." }
            loading = false
        }
    }
    if (settingsOpen && selectedConversation != null && chatBootstrap != null) ChatSettingsDialog(
        conversation = selectedConversation!!,
        bootstrap = chatBootstrap!!,
        onDismiss = { settingsOpen = false },
        onSave = { memoryEnabled, botId ->
            scope.launch {
                runCatching {
                    withContext(Dispatchers.IO) {
                        api.updateMemory(selectedConversation!!.id, memoryEnabled)
                        api.updateRoute(selectedConversation!!.id, botId)
                    }
                }.onSuccess { updated ->
                    selectedConversation = updated
                    conversations = conversations.map { if (it.id == updated.id) updated else it }
                    settingsOpen = false
                }.onFailure { status = it.message ?: "Could not save chat settings." }
            }
        },
        onArchive = {
            scope.launch {
                runCatching { withContext(Dispatchers.IO) { api.archiveConversation(selectedConversation!!.id) } }
                    .onSuccess { selectedConversation = null; messages = emptyList(); settingsOpen = false; loadConversations() }
                    .onFailure { status = it.message ?: "Could not archive chat." }
            }
        },
        onDelete = {
            scope.launch {
                runCatching { withContext(Dispatchers.IO) { api.deleteConversation(selectedConversation!!.id) } }
                    .onSuccess { selectedConversation = null; messages = emptyList(); settingsOpen = false; loadConversations() }
                    .onFailure { status = it.message ?: "Could not delete chat." }
            }
        },
    )
    if (appSettingsOpen) AppSettingsDialog(
        instanceUrl = instanceUrl,
        theme = theme,
        update = update,
        onDismiss = { appSettingsOpen = false },
        onThemeChange = onThemeChange,
        onInstallUpdate = onInstallUpdate,
        onChangeInstance = onChangeInstance,
    )
}

@Composable
private fun ConversationScreen(modifier: Modifier, conversation: ChatConversation, messages: List<ChatMessage>, loading: Boolean, status: String, onBack: () -> Unit, onRefresh: () -> Unit, onSettings: () -> Unit, onSend: (String) -> Unit) {
    var draft by remember { mutableStateOf("") }
    Column(modifier, verticalArrangement = Arrangement.spacedBy(10.dp)) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Button(onClick = onBack) { Text("Chats") }
        Text(conversation.title, style = MaterialTheme.typography.titleMedium)
        Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            Button(onClick = onRefresh) { Text("Refresh") }
            Button(onClick = onSettings) { Text("Settings") }
        }
    }
    LazyColumn(Modifier.fillMaxWidth().height(360.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        items(messages) { message ->
            Column(Modifier.fillMaxWidth()) {
                Text(if (message.role == "user") "You" else "NexusAI", style = MaterialTheme.typography.labelMedium)
                Text(message.content)
            }
        }
    }
    OutlinedTextField(value = draft, onValueChange = { draft = it }, label = { Text("Message") }, modifier = Modifier.fillMaxWidth(), minLines = 2, enabled = !loading)
    Button(enabled = !loading && draft.isNotBlank(), onClick = { onSend(draft.trim()); draft = "" }, modifier = Modifier.fillMaxWidth()) { Text(if (loading) "Sending…" else "Send") }
    if (status.isNotBlank()) Text(status, color = MaterialTheme.colorScheme.error)
    }
}

@Composable
private fun ChatSettingsDialog(conversation: ChatConversation, bootstrap: ChatBootstrap, onDismiss: () -> Unit, onSave: (Boolean, String?) -> Unit, onArchive: () -> Unit, onDelete: () -> Unit) {
    var memoryEnabled by remember { mutableStateOf(conversation.memoryEnabled) }
    var botId by remember { mutableStateOf(conversation.defaultBotId.orEmpty()) }
    var deleteConfirmation by remember { mutableStateOf(false) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Chat settings") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("Use memory")
                    Switch(checked = memoryEnabled, onCheckedChange = { memoryEnabled = it })
                }
                Text("Bot ID", style = MaterialTheme.typography.labelMedium)
                OutlinedTextField(value = botId, onValueChange = { botId = it }, modifier = Modifier.fillMaxWidth(), supportingText = { Text(bootstrap.bots.joinToString { it.id }) })
                Text("The selected bot's configured backend model is used for this conversation.", style = MaterialTheme.typography.bodySmall)
                Button(onClick = onArchive, modifier = Modifier.fillMaxWidth()) { Text("Archive chat") }
                Button(onClick = { deleteConfirmation = true }, modifier = Modifier.fillMaxWidth()) { Text("Delete chat") }
                if (deleteConfirmation) Text("Delete is permanent. Tap Delete chat again to confirm.", color = MaterialTheme.colorScheme.error)
            }
        },
        confirmButton = {
            Button(onClick = {
            if (deleteConfirmation) onDelete() else onSave(memoryEnabled, botId.ifBlank { null })
            }) { Text(if (deleteConfirmation) "Delete chat" else "Save") }
        },
        dismissButton = { Button(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun AppSettingsDialog(
    instanceUrl: String,
    theme: AppTheme,
    update: AndroidUpdate?,
    onDismiss: () -> Unit,
    onThemeChange: (AppTheme) -> Unit,
    onInstallUpdate: () -> Unit,
    onChangeInstance: () -> Unit,
) = AlertDialog(
    onDismissRequest = onDismiss,
    title = { Text("App settings") },
    text = {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Instance", style = MaterialTheme.typography.labelMedium)
            Text(instanceUrl, style = MaterialTheme.typography.bodySmall)
            Text("Appearance", style = MaterialTheme.typography.labelMedium)
            AppTheme.entries.forEach { option ->
                Row(Modifier.fillMaxWidth().clickable { onThemeChange(option) }, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    RadioButton(selected = theme == option, onClick = { onThemeChange(option) })
                    Text(option.name.lowercase().replaceFirstChar { it.uppercase() })
                }
            }
            Text("App version ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})", style = MaterialTheme.typography.bodySmall)
            Text("Build ${BuildConfig.BUILD_COMMIT}", style = MaterialTheme.typography.bodySmall)
            if (update != null) {
                Text("Update ${update.latestVersionCode} is available.", style = MaterialTheme.typography.bodySmall)
                Button(onClick = onInstallUpdate, modifier = Modifier.fillMaxWidth()) { Text("Install update") }
            } else {
                Text("No newer update is currently advertised by this instance.", style = MaterialTheme.typography.bodySmall)
            }
            Button(onClick = onChangeInstance, modifier = Modifier.fillMaxWidth()) { Text("Change instance") }
        }
    },
    confirmButton = { Button(onClick = onDismiss) { Text("Close") } },
)

@Composable
private fun ProjectPicker(projects: List<ChatProject>, selected: ChatProject?, onDismiss: () -> Unit, onSelect: (ChatProject?) -> Unit) = AlertDialog(
    onDismissRequest = onDismiss,
    title = { Text("Chat scope") },
    text = { Column { Button(onClick = { onSelect(null) }, modifier = Modifier.fillMaxWidth()) { Text("Unscoped chats") }; projects.forEach { project -> Button(onClick = { onSelect(project) }, modifier = Modifier.fillMaxWidth()) { Text(project.name) } } } },
    confirmButton = { Button(onClick = onDismiss) { Text("Close") } },
)

@Composable
private fun NewChatDialog(project: ChatProject?, onDismiss: () -> Unit, onCreate: (String) -> Unit) {
    var title by remember { mutableStateOf("") }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("New ${project?.name ?: "unscoped"} chat") }, text = { OutlinedTextField(value = title, onValueChange = { title = it }, label = { Text("Chat title") }, singleLine = true) }, confirmButton = { Button(enabled = title.isNotBlank(), onClick = { onCreate(title.trim()) }) { Text("Create") } }, dismissButton = { Button(onClick = onDismiss) { Text("Cancel") } })
}

@Composable
private fun CenterMessage(modifier: Modifier, message: String) = Column(modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.Center) { Text(message) }

@Composable
private fun ConnectionScreen(modifier: Modifier, onConnect: (String) -> Unit) {
    var url by remember { mutableStateOf("") }
    Column(modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text("Connect your NexusAI instance", style = MaterialTheme.typography.headlineSmall)
        Text("Enter the HTTPS address for the NexusAI deployment you own or administer.")
        OutlinedTextField(value = url, onValueChange = { url = it }, label = { Text("NexusAI URL") }, placeholder = { Text("https://chat.example.com") }, modifier = Modifier.fillMaxWidth(), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), singleLine = true)
        Button(onClick = { onConnect(url) }) { Text("Continue") }
    }
}

@Composable
private fun LoginScreen(modifier: Modifier, instanceUrl: String, status: String, loading: Boolean, onSignIn: (String, String) -> Unit, onChangeInstance: () -> Unit) {
    var email by remember { mutableStateOf("") }; var password by remember { mutableStateOf("") }
    Column(modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Text("Sign in", style = MaterialTheme.typography.headlineSmall); Text(instanceUrl, style = MaterialTheme.typography.bodySmall)
        OutlinedTextField(email, { email = it }, label = { Text("Email") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(password, { password = it }, label = { Text("Password") }, modifier = Modifier.fillMaxWidth(), singleLine = true, visualTransformation = PasswordVisualTransformation(), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password))
        Button(enabled = !loading && email.isNotBlank() && password.isNotBlank(), onClick = { onSignIn(email, password) }) { Text(if (loading) "Signing in" else "Sign in") }
        Button(enabled = !loading, onClick = onChangeInstance) { Text("Change instance") }
        if (status.isNotBlank()) Text(status, color = MaterialTheme.colorScheme.error)
    }
}
