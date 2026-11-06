cmake_minimum_required(VERSION 3.22.1)
project(BleHubReactNativeBleng)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_VERBOSE_MAKEFILE ON)

# System.loadLibrary needs this JNI entry point to invoke Nitrogen's registration.
# BLeng/UniFFI remains behind the Kotlin bridge, not a hand-written C++ ABI.
add_library(BleHubReactNativeBleng SHARED src/main/cpp/cpp-adapter.cpp)

include(${CMAKE_SOURCE_DIR}/../nitrogen/generated/android/BleHubReactNativeBleng+autolinking.cmake)

find_library(log-lib log)

target_link_libraries(
  BleHubReactNativeBleng
  ${log-lib}
  android
)
