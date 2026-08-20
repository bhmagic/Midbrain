#pragma once

#include <cuda_runtime.h>

#include <cstdint>

namespace midbrain::foundation_pose
{
void convertU8ToFloat(
  cudaStream_t stream, const std::uint8_t * input, float * output,
  std::size_t element_count, float scale);

void flipVerticalFloat(
  cudaStream_t stream, const float * input, float * output,
  int batch, int height, int width, int channels);

void warpPerspectiveU8(
  cudaStream_t stream, const std::uint8_t * input, int input_height, int input_width,
  std::uint8_t * output, int output_height, int output_width, int channels,
  const float * destination_to_source_matrices, int batch);

void warpPerspectiveFloatNearest(
  cudaStream_t stream, const float * input, int input_height, int input_width,
  float * output, int output_height, int output_width, int channels,
  const float * destination_to_source_matrices, int batch);
}  // namespace midbrain::foundation_pose
