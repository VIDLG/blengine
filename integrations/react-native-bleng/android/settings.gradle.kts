pluginManagement {
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/gradle-plugin")
        maven("https://maven.aliyun.com/repository/public")
        gradlePluginPortal()
    }
    plugins {
        id("com.android.library") version "8.10.1"
        id("org.jetbrains.kotlin.android") version "2.1.20"
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    repositories {
        maven { url = uri("../../../.work/staged/android-bleng-maven") }
        maven { url = uri("../../../.work/staged/android-maven") }
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
    }
}

rootProject.name = "blehub-react-native-bleng-android"
include(":react-native-nitro-modules")
project(":react-native-nitro-modules").projectDir = file("../node_modules/react-native-nitro-modules/android")
