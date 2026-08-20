cmake_minimum_required(VERSION 3.24)

if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)
  set(CMAKE_CUDA_ARCHITECTURES 120 CACHE STRING "CUDA architectures to compile")
endif()

project(midbrain_foundation_pose_native VERSION 1.0.0 LANGUAGES CXX CUDA)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_STANDARD 17)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)

if(NOT WIN32)
  message(FATAL_ERROR "The extracted Midbrain FoundationPose runtime is a native Windows target")
endif()

find_package(CUDAToolkit 12.8 REQUIRED)

set(UPSTREAM_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/upstream/isaac_ros_foundationpose")
set(UPSTREAM_IMPL "${UPSTREAM_ROOT}/src/foundationpose_impl")
set(NVDIFFRAST_ROOT "${UPSTREAM_ROOT}/src/nvdiffrast")

add_library(midbrain_foundation_pose_native SHARED
  src/native_api.cpp
  src/windows_preprocess.cu
  ${UPSTREAM_IMPL}/mesh_loader.cpp
  ${UPSTREAM_IMPL}/pose_sampler.cpp
  ${UPSTREAM_IMPL}/pose_renderer.cpp
  ${UPSTREAM_IMPL}/pose_transformer.cpp
  ${UPSTREAM_IMPL}/foundationpose_sampling.cu
  ${UPSTREAM_IMPL}/foundationpose_render.cu
  ${NVDIFFRAST_ROOT}/common/common.cpp
  ${NVDIFFRAST_ROOT}/common/interpolate.cu
  ${NVDIFFRAST_ROOT}/common/rasterize.cu
  ${NVDIFFRAST_ROOT}/common/texture.cu
  ${NVDIFFRAST_ROOT}/common/cudaraster/impl/Buffer.cpp
  ${NVDIFFRAST_ROOT}/common/cudaraster/impl/CudaRaster.cpp
  ${NVDIFFRAST_ROOT}/common/cudaraster/impl/RasterImpl.cpp
  ${NVDIFFRAST_ROOT}/common/cudaraster/impl/RasterImpl.cu
)

target_compile_definitions(midbrain_foundation_pose_native PRIVATE
  MIDBRAIN_FOUNDATION_POSE_EXPORTS
  NOMINMAX
  _USE_MATH_DEFINES
)
target_include_directories(midbrain_foundation_pose_native PUBLIC
  "${CMAKE_CURRENT_SOURCE_DIR}/include"
  "${UPSTREAM_ROOT}/include"
  "${UPSTREAM_IMPL}"
  "${UPSTREAM_ROOT}/src"
  "${NVDIFFRAST_ROOT}/common"
  "${NVDIFFRAST_ROOT}/common/cudaraster"
  "${NVDIFFRAST_ROOT}/common/cudaraster/impl"
  "${CMAKE_CURRENT_SOURCE_DIR}/third_party/eigen"
)
target_link_libraries(midbrain_foundation_pose_native PRIVATE CUDA::cudart)
target_compile_options(midbrain_foundation_pose_native PRIVATE
  $<$<COMPILE_LANGUAGE:CXX>:/W4 /permissive->
  $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=/W3>
)
set_target_properties(midbrain_foundation_pose_native PROPERTIES
  CUDA_SEPARABLE_COMPILATION ON
  OUTPUT_NAME "midbrain_foundation_pose_native"
)

add_executable(midbrain_foundation_pose_native_probe src/native_probe.cpp)
target_link_libraries(midbrain_foundation_pose_native_probe PRIVATE
  midbrain_foundation_pose_native
)

install(TARGETS midbrain_foundation_pose_native midbrain_foundation_pose_native_probe
  RUNTIME DESTINATION bin
  LIBRARY DESTINATION bin
  ARCHIVE DESTINATION lib
)
