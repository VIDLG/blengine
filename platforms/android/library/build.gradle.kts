plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
    id("maven-publish")
}

android {
    namespace = "dev.blehub.android"
    compileSdk = 36
    buildToolsVersion = "36.0.0"
    ndkVersion = "28.2.13676358"

    defaultConfig {
        minSdk = 24
        consumerProguardFiles("consumer-rules.pro")
    }


    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    publishing {
        singleVariant("release")
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    api("dev.blehub:blehub-uniffi:0.0.0")
    api("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
    implementation("androidx.annotation:annotation:1.9.1")
    testImplementation(kotlin("test"))
}

publishing {
    publications {
        register<MavenPublication>("release") {
            groupId = "dev.blehub"
            artifactId = "blehub-android"
            version = "0.0.0"
            afterEvaluate {
                from(components["release"])
            }
        }
    }
    repositories {
        maven {
            name = "fixture"
            url = uri(file("../../../.work/staged/android-maven"))
        }
    }
}
