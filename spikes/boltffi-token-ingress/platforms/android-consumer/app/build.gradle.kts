plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val bindingsAar = file("../../android/bindings/build/outputs/aar/bindings-debug.aar")
if (!bindingsAar.isFile) {
    error("missing Phase 0 bindings AAR: $bindingsAar")
}

android {
    namespace = "dev.blehub.phase0.consumer"
    compileSdk = 36
    buildToolsVersion = "36.0.0"

    defaultConfig {
        applicationId = "dev.blehub.phase0.consumer"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "0.0.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        animationsDisabled = true
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    implementation(files(bindingsAar))
    androidTestImplementation("androidx.test.ext:junit-ktx:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
}
