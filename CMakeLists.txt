# Op-level compile-time collection for MLIR-based compilers: header-only
# collector plus a Python aggregation script.  This directory is
# self-contained and can be copied out of this repository.
#
# Embedded (as in this repository):
#   add_subdirectory(op-timing)
#   target_link_libraries(<your-tool> PRIVATE op-timing)
#
# Standalone:
#   cmake -S op-timing -B build-op-timing -DMLIR_DIR=<mlir cmake dir>
#   cmake --build build-op-timing

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

# MLIRIR for MLIRContext / Action / Operation, MLIRSupport for the
# instrumentation helpers.  Nothing else is required: the collector uses
# public MLIR API only, so no patched LLVM checkout is needed.
target_link_libraries(op-timing INTERFACE
  MLIRIR
  MLIRSupport)

if(OP_TIMING_STANDALONE)
  file(GLOB OP_TIMING_HEADERS ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h)
  add_library(op-timing-syntax-check OBJECT
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/syntax-check.cpp)
  target_link_libraries(op-timing-syntax-check PRIVATE op-timing)
  set_target_properties(op-timing-syntax-check PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON)
endif()
