plugins {
    kotlin("jvm") version "2.1.0"
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
    implementation("net.java.dev.jna:jna:5.18.1")
}
