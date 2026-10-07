import org.gradle.api.artifacts.component.ProjectComponentIdentifier

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.blehub.bleng.consumer"
    compileSdk = 36
    buildToolsVersion = "36.0.0"

    defaultConfig {
        applicationId = "dev.blehub.bleng.consumer"
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
    implementation("dev.blehub:bleng:0.0.0")
    androidTestImplementation("androidx.test.ext:junit-ktx:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation(kotlin("test"))
}

val verifyBlengArtifact by tasks.registering {
    doLast {
        val runtime = configurations.getByName("debugRuntimeClasspath")
        val projectDependencies = runtime.incoming.resolutionResult.allComponents.filter {
            it.id is ProjectComponentIdentifier && it.id != runtime.incoming.resolutionResult.root.id
        }
        check(projectDependencies.isEmpty()) { "consumer must not resolve source project dependencies" }
        val blengArtifacts = runtime.resolvedConfiguration.resolvedArtifacts.filter {
            it.moduleVersion.id.group == "dev.blehub"
        }
        check(blengArtifacts.size == 1) { "expected only the isolated BLeng Maven artifact" }
        val artifact = blengArtifacts.single()
        check(artifact.moduleVersion.id.name == "bleng" && artifact.type == "aar")
        val staged = rootProject.file("../../../.work/staged/android-bleng-maven/dev/blehub/bleng/0.0.0/bleng-0.0.0.aar")
        check(artifact.file.canonicalFile == staged.canonicalFile) {
            "consumer resolved BLeng package outside the isolated Maven staging root: ${artifact.file}"
        }
        println("bleng_artifact_only=${artifact.file}")
    }
}

tasks.named("preBuild") {
    dependsOn(verifyBlengArtifact)
}
