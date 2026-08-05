# NexusAI Android Client

This native Android client connects to a user-owned NexusAI dashboard deployment. It does not require or contact a shared NexusAI service.

## Current Scope

- HTTPS instance onboarding.
- Dashboard account sign-in through `/api/auth/login`.
- Encrypted local storage for the instance URL and dashboard session cookie.
- Session restoration when the app is reopened.
- Instance-driven update checks through `/api/mobile/bootstrap`.
- Project and unscoped conversation selection, new chat creation, message history, refresh, and normal text messages.

File uploads, streamed tokens, chat settings, work monitoring, notifications, and agentic controls are follow-up milestones. The first client intentionally sends only normal text messages and does not expose worker, repository, deployment, or automation controls.

## Updating Without Reinstalling

Publish each release APK from an HTTPS URL controlled by the self-hosted instance, then set these dashboard environment variables before deploying the dashboard:

```env
NEXUSAI_MOBILE_ANDROID_MIN_VERSION_CODE=1
NEXUSAI_MOBILE_ANDROID_LATEST_VERSION_CODE=2
NEXUSAI_MOBILE_ANDROID_RELEASE_URL=https://chat.example.com/releases/nexusai-android.apk
```

The app compares these version codes with its installed version. When an update is available, it downloads the configured APK and opens Android's standard installer. Android replaces the existing NexusAI package in place only when the APK uses the same application ID and signing key, preserving the configured instance and encrypted local session. The user must approve installation; Android does not allow an ordinary app to silently replace itself.

Keep the APK URL on HTTPS and retain the same release signing key. A mismatched signature is rejected by Android rather than replacing the app.

## Self-Hosted Release Publishing

The persistent signing material is intentionally stored outside the repository at `%USERPROFILE%\.nexusai\android-release`. Publish without GitHub Actions:

```powershell
.\scripts\publish-android-release.ps1 -ReleaseTarget 'jacob@your-server:/srv/nexusai/releases/nexusai.apk'
```

Point `NEXUSAI_MOBILE_ANDROID_RELEASE_URL` at that HTTPS-served file and increase `NEXUSAI_MOBILE_ANDROID_LATEST_VERSION_CODE` for every published build. The app checks after sign-in and on launch, downloads an available update automatically, and asks Android for the one-time NexusAI installer permission if needed.

## Build

Install Android SDK platform 36 and JDK 17. On Windows PowerShell:

```powershell
$env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
.\gradlew.bat :app:assembleDebug
```

The resulting APK is `app/build/outputs/apk/debug/app-debug.apk`.

## Connection Security

The app accepts HTTPS instance URLs only. It stores the configured instance URL and dashboard session cookie using Android encrypted preferences. It never receives the control-plane token, provider credentials, or other server secrets.
