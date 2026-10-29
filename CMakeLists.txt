# Header-only collector plus a Python aggregation script; self-contained, so
# this directory can be copied into another compiler repository.
#
# Embedded:    add_subdirectory(<path-to-op-timing>)  +  link target op-timing
# Standalone:  cmake -S . -B <build> -DMLIR_DIR=<mlir cmake dir>
# Docs:        README.md, docs/design.md, docs/usage.md

cmake_minimum_required(VERSION 3.20)

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  project(OpTiming LANGUAGES CXX)
  set(OP_TIMING_STANDALONE ON)

  find_package(MLIR REQUIRED CONFIG)
  message(STATUS "OpTiming: using MLIR from ${MLIR_DIR}")
endif()

add_library(op-timing INTERFACE)
add_library(optiming::op-timing ALIAS op-timing)

target_include_directories(op-timing INTERFACE
  ${CMAKE_CURRENT_SOURCE_DIR}/include)

# Public MLIR API only, so no patched LLVM checkout is required.
target_link_libraries(op-timing INTERFACE
  MLIRIR
  MLIRSupport)

if(OP_TIMING_STANDALONE)
  add_library(op-timing-syntax-check OBJECT
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/syntax-check.cpp)
  target_link_libraries(op-timing-syntax-check PRIVATE op-timing)
  set_target_properties(op-timing-syntax-check PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON)
endif()
