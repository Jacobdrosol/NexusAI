# NexusAI Android Client

This native Android client connects to a user-owned NexusAI dashboard deployment. It does not require or contact a shared NexusAI service.

## Current Scope

- HTTPS instance onboarding.
- Dashboard account sign-in through `/api/auth/login`.
- Encrypted local storage for the instance URL and dashboard session cookie.
- Session restoration when the app is reopened.
- Instance-driven update checks through `/api/mobile/bootstrap`.
- Project and unscoped conversation selection, new chat creation, message history, refresh, and normal text messages.
- A compact chat menu for new chats, refresh, chat settings, app settings, and sign-out.
- Per-message `...` actions. The current native action is Copy; server-side message mutations remain unavailable in the Android client.
- App settings for the connected instance, persistent system/dark/light appearance, installed version/build, and advertised updates.

File uploads, streamed tokens, work monitoring, notifications, and agentic controls are follow-up milestones. The first client intentionally sends only normal text messages and does not expose worker, repository, deployment, or automation controls.

## Updating Without Reinstalling

The deployment workflow publishes the signed APK to the self-hosted instance's `/releases/nexusai.apk` endpoint and writes `data/mobile-release.json`. The dashboard exposes that manifest through `/api/mobile/bootstrap`; no release values need to be manually maintained in `.env`.

Increment `versionCode` in `android/app/build.gradle.kts` for each mobile release, commit it, and run the normal NexusAI deployment. The deployment script compares the version to the manifest, builds only when a newer version is needed, replaces the hosted APK, and updates the manifest. The app checks after launch/session restoration and after sign-in.

When an update is available, the app downloads it and opens Android's standard installer. Android replaces the existing NexusAI package in place only when the APK uses the same application ID and signing key, preserving the configured instance, appearance preference, and encrypted local session cookie. The app does not store the user's password. It validates the stored session at launch, so a server-expired or revoked session still requires sign-in. The user must approve installation; an ordinary Android app cannot silently replace itself.

Keep the APK endpoint on HTTPS and retain the same release signing key. A mismatched signature is rejected by Android rather than replacing the app.

## Self-Hosted Release Publishing

The release signing material is stored outside the repository. On a self-hosted instance, place it at `data/android-signing/` and configure the secret values in `data/android-signing/release-signing.env`. The deployment publisher mounts this directory read-only into its Android build container. No GitHub Actions runner is required.

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
