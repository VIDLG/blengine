pluginManagement {
    repositories {
        maven("https://mirrors.huaweicloud.com/repository/maven/")
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven("https://mirrors.huaweicloud.com/repository/maven/")
    }
}

rootProject.name = "blehub-uniffi-runtime-android"
include(":bindings")
