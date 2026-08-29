plugins {
    kotlin("jvm") version "2.1.0"
}

base {
    archivesName.set("blehub-phase0-token-ingress-bindings")
}

kotlin {
    jvmToolchain(17)
}

sourceSets {
    main {
        kotlin.srcDir("../../generated/kotlin")
    }
}

dependencies {
    testImplementation(kotlin("test"))
}

val nativeDirectory = projectDir.resolve("../../../..")
    .resolve(".work/jvm-native")
    .canonicalPath

tasks.test {
    useJUnitPlatform()
    jvmArgs("-Djava.library.path=$nativeDirectory")
}
