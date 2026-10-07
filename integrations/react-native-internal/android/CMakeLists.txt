cmake_minimum_required(VERSION 3.22.1)
project(BleHubReactNative)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_VERBOSE_MAKEFILE ON)

add_library(BleHubReactNative SHARED
  src/main/cpp/cpp-adapter.cpp
)

include(${CMAKE_SOURCE_DIR}/../nitrogen/generated/android/BleHubReactNative+autolinking.cmake)

find_library(log-lib log)

target_link_libraries(
  BleHubReactNative
  ${log-lib}
  android
)
