plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
    id("maven-publish")
}

android {
    namespace = "dev.blehub.bleng.bindings"
    compileSdk = 36
    buildToolsVersion = "36.0.0"
    ndkVersion = "28.2.13676358"

    defaultConfig {
        minSdk = 24
        consumerProguardFiles("consumer-rules.pro")
    }

    sourceSets.getByName("main") {
        java.srcDir("../../../../../.work/android-bleng/generated/kotlin")
        jniLibs.srcDir("../../../../../.work/android-bleng/generated/jni")
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
    api("net.java.dev.jna:jna:5.18.1@aar")
    api("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
    implementation("androidx.annotation:annotation:1.9.1")
}

publishing {
    publications {
        register<MavenPublication>("release") {
            groupId = "dev.blehub"
            artifactId = "bleng"
            version = "0.0.0"
            afterEvaluate {
                from(components["release"])
            }
        }
    }
    repositories {
        maven {
            name = "bleng"
            url = uri(file("../../../../../.work/staged/android-bleng-maven"))
        }
    }
}
