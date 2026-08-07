plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

val signingStoreFile = providers.environmentVariable("NEXUSAI_ANDROID_STORE_FILE").orNull
val signingStorePassword = providers.environmentVariable("NEXUSAI_ANDROID_STORE_PASSWORD").orNull
val signingKeyAlias = providers.environmentVariable("NEXUSAI_ANDROID_KEY_ALIAS").orNull
val signingKeyPassword = providers.environmentVariable("NEXUSAI_ANDROID_KEY_PASSWORD").orNull
val buildCommit = providers.environmentVariable("NEXUSAI_ANDROID_GIT_SHA").orNull ?: "local"

android {
    namespace = "org.nexusai.mobile"
    compileSdk = 36

    defaultConfig {
        applicationId = "org.nexusai.mobile"
        minSdk = 26
        targetSdk = 36
        versionCode = 6
        versionName = "0.2.4"
        buildConfigField("String", "BUILD_COMMIT", "\"$buildCommit\"")
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    buildTypes {
        release {
            if (signingStoreFile != null && signingStorePassword != null && signingKeyAlias != null && signingKeyPassword != null) {
                signingConfig = signingConfigs.create("release") {
                    storeFile = file(signingStoreFile)
                    storePassword = signingStorePassword
                    keyAlias = signingKeyAlias
                    keyPassword = signingKeyPassword
                }
            }
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2026.06.00"))
    implementation("androidx.activity:activity-compose:1.12.4")
    implementation("androidx.core:core-ktx:1.17.0")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.10.0")
    implementation("androidx.security:security-crypto:1.1.0")
    implementation("com.squareup.okhttp3:okhttp:5.1.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    debugImplementation("androidx.compose.ui:ui-tooling")
}
