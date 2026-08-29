plugins {
    kotlin("jvm") version "2.1.0"
}

kotlin {
    jvmToolchain(17)
}

val bindingsJar = projectDir.resolve(
    "../kotlin-jvm/build/libs/blehub-phase0-token-ingress-bindings.jar",
)

if (!bindingsJar.isFile) {
    throw GradleException("missing generated bindings artifact: $bindingsJar")
}

dependencies {
    testImplementation(files(bindingsJar))
    testImplementation(kotlin("test"))
}

val nativeDirectory = projectDir.resolve("../../../..")
    .resolve(".work/jvm-native")
    .canonicalPath

tasks.test {
    useJUnitPlatform()
    jvmArgs("-Djava.library.path=$nativeDirectory")
}
