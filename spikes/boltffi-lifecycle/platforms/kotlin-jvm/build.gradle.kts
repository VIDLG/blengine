plugins {
    kotlin("jvm") version "2.1.0"
}

repositories {
    mavenCentral()
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

tasks.register<JavaExec>("sinkExceptionProbe") {
    dependsOn(tasks.testClasses)
    classpath = sourceSets.test.get().runtimeClasspath
    mainClass.set("dev.blehub.spike.SinkExceptionProbe")
    jvmArgs("-Djava.library.path=$nativeDirectory")
}
