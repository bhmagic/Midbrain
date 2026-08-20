#include "midbrain_foundation_pose/windows_preprocess.hpp"

#include "isaac_ros_common/cuda_stream.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace midbrain::foundation_pose
{
namespace
{
constexpr int kThreads = 256;

__global__ void convertU8ToFloatKernel(
  const std::uint8_t * input, float * output, std::size_t count, float scale)
{
  const std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index < count) {
    output[index] = static_cast<float>(input[index]) * scale;
  }
}

__global__ void flipVerticalFloatKernel(
  const float * input, float * output, int height, int width, int channels,
  std::size_t count)
{
  const std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= count) {
    return;
  }
  const int channel = static_cast<int>(index % channels);
  const std::size_t pixel = index / channels;
  const int x = static_cast<int>(pixel % width);
  const std::size_t row = pixel / width;
  const int y = static_cast<int>(row % height);
  const std::size_t batch_index = row / height;
  const int source_y = height - 1 - y;
  const std::size_t source =
    (((batch_index * height + source_y) * width + x) * channels) + channel;
  output[index] = input[source];
}

__device__ bool mapDestinationToSource(
  const float * matrix, int x, int y, float & source_x, float & source_y)
{
  const float denominator = matrix[6] * x + matrix[7] * y + matrix[8];
  if (fabsf(denominator) < 1.0e-8f) {
    return false;
  }
  source_x = (matrix[0] * x + matrix[1] * y + matrix[2]) / denominator;
  source_y = (matrix[3] * x + matrix[4] * y + matrix[5]) / denominator;
  return isfinite(source_x) && isfinite(source_y);
}

__global__ void warpPerspectiveU8Kernel(
  const std::uint8_t * input, int input_height, int input_width,
  std::uint8_t * output, int output_height, int output_width, int channels,
  const float * matrices, std::size_t pixel_count)
{
  const std::size_t pixel = blockIdx.x * blockDim.x + threadIdx.x;
  if (pixel >= pixel_count) {
    return;
  }
  const int x = static_cast<int>(pixel % output_width);
  const std::size_t row = pixel / output_width;
  const int y = static_cast<int>(row % output_height);
  const std::size_t batch_index = row / output_height;
  const float * matrix = matrices + batch_index * 9;
  float source_x = 0.0f;
  float source_y = 0.0f;
  std::uint8_t * target = output + pixel * channels;
  if (!mapDestinationToSource(matrix, x, y, source_x, source_y) ||
    source_x < 0.0f || source_y < 0.0f ||
    source_x > input_width - 1 || source_y > input_height - 1)
  {
    for (int channel = 0; channel < channels; ++channel) {
      target[channel] = 0;
    }
    return;
  }

  const int x0 = static_cast<int>(floorf(source_x));
  const int y0 = static_cast<int>(floorf(source_y));
  const int x1 = min(x0 + 1, input_width - 1);
  const int y1 = min(y0 + 1, input_height - 1);
  const float wx = source_x - x0;
  const float wy = source_y - y0;
  for (int channel = 0; channel < channels; ++channel) {
    const float p00 = input[(y0 * input_width + x0) * channels + channel];
    const float p10 = input[(y0 * input_width + x1) * channels + channel];
    const float p01 = input[(y1 * input_width + x0) * channels + channel];
    const float p11 = input[(y1 * input_width + x1) * channels + channel];
    const float top = p00 + wx * (p10 - p00);
    const float bottom = p01 + wx * (p11 - p01);
    target[channel] = static_cast<std::uint8_t>(
      fminf(255.0f, fmaxf(0.0f, top + wy * (bottom - top) + 0.5f)));
  }
}

__global__ void warpPerspectiveFloatNearestKernel(
  const float * input, int input_height, int input_width,
  float * output, int output_height, int output_width, int channels,
  const float * matrices, std::size_t pixel_count)
{
  const std::size_t pixel = blockIdx.x * blockDim.x + threadIdx.x;
  if (pixel >= pixel_count) {
    return;
  }
  const int x = static_cast<int>(pixel % output_width);
  const std::size_t row = pixel / output_width;
  const int y = static_cast<int>(row % output_height);
  const std::size_t batch_index = row / output_height;
  const float * matrix = matrices + batch_index * 9;
  float source_x = 0.0f;
  float source_y = 0.0f;
  float * target = output + pixel * channels;
  if (!mapDestinationToSource(matrix, x, y, source_x, source_y)) {
    for (int channel = 0; channel < channels; ++channel) {
      target[channel] = 0.0f;
    }
    return;
  }
  const int nearest_x = static_cast<int>(floorf(source_x + 0.5f));
  const int nearest_y = static_cast<int>(floorf(source_y + 0.5f));
  if (nearest_x < 0 || nearest_y < 0 || nearest_x >= input_width || nearest_y >= input_height) {
    for (int channel = 0; channel < channels; ++channel) {
      target[channel] = 0.0f;
    }
    return;
  }
  const float * source = input + (nearest_y * input_width + nearest_x) * channels;
  for (int channel = 0; channel < channels; ++channel) {
    target[channel] = source[channel];
  }
}
}  // namespace

void convertU8ToFloat(
  cudaStream_t stream, const std::uint8_t * input, float * output,
  std::size_t element_count, float scale)
{
  const int blocks = static_cast<int>((element_count + kThreads - 1) / kThreads);
  convertU8ToFloatKernel<<<blocks, kThreads, 0, stream>>>(input, output, element_count, scale);
  CHECK_CUDA_ERROR(cudaGetLastError(), "convertU8ToFloatKernel");
}

void flipVerticalFloat(
  cudaStream_t stream, const float * input, float * output,
  int batch, int height, int width, int channels)
{
  const std::size_t count =
    static_cast<std::size_t>(batch) * height * width * channels;
  const int blocks = static_cast<int>((count + kThreads - 1) / kThreads);
  flipVerticalFloatKernel<<<blocks, kThreads, 0, stream>>>(
    input, output, height, width, channels, count);
  CHECK_CUDA_ERROR(cudaGetLastError(), "flipVerticalFloatKernel");
}

void warpPerspectiveU8(
  cudaStream_t stream, const std::uint8_t * input, int input_height, int input_width,
  std::uint8_t * output, int output_height, int output_width, int channels,
  const float * destination_to_source_matrices, int batch)
{
  const std::size_t pixels =
    static_cast<std::size_t>(batch) * output_height * output_width;
  const int blocks = static_cast<int>((pixels + kThreads - 1) / kThreads);
  warpPerspectiveU8Kernel<<<blocks, kThreads, 0, stream>>>(
    input, input_height, input_width, output, output_height, output_width,
    channels, destination_to_source_matrices, pixels);
  CHECK_CUDA_ERROR(cudaGetLastError(), "warpPerspectiveU8Kernel");
}

void warpPerspectiveFloatNearest(
  cudaStream_t stream, const float * input, int input_height, int input_width,
  float * output, int output_height, int output_width, int channels,
  const float * destination_to_source_matrices, int batch)
{
  const std::size_t pixels =
    static_cast<std::size_t>(batch) * output_height * output_width;
  const int blocks = static_cast<int>((pixels + kThreads - 1) / kThreads);
  warpPerspectiveFloatNearestKernel<<<blocks, kThreads, 0, stream>>>(
    input, input_height, input_width, output, output_height, output_width,
    channels, destination_to_source_matrices, pixels);
  CHECK_CUDA_ERROR(cudaGetLastError(), "warpPerspectiveFloatNearestKernel");
}
}  // namespace midbrain::foundation_pose
