pluginManagement {
    repositories {
        maven("https://mirrors.huaweicloud.com/repository/maven/")
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven(url = uri(file("../../.work/staged/android-maven")))
        maven("https://mirrors.huaweicloud.com/repository/maven/")
    }
}

rootProject.name = "blehub-android"
include(":library")
