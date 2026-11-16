plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Every CI build gets a higher version number than the one before, which is how
// a phone knows the box holds a newer build. A build on a desk is 1.
val buildNumber = (System.getenv("GITHUB_RUN_NUMBER") ?: "").toIntOrNull()

android {
    namespace = "nz.co.asiltd.kentrackit"
    compileSdk = 35

    defaultConfig {
        applicationId = "nz.co.asiltd.kentrackit"
        minSdk = 26
        targetSdk = 35
        versionCode = buildNumber ?: 1
        versionName = if (buildNumber != null) "1.0.$buildNumber" else "1.0.0"
    }

    signingConfigs {
        // A fixed key kept in the repo, so every build installs over the previous
        // one without losing settings. It is not a Play Store release key.
        getByName("debug") {
            storeFile = file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("debug")
        }
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    lint {
        // lint runs on demand, not as a gate on every build
        checkReleaseBuilds = false
        abortOnError = false
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    testImplementation("junit:junit:4.13.2")
}
