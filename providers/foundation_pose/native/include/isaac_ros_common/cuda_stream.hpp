#pragma once

#include <cuda_runtime.h>

#include <sstream>
#include <stdexcept>
#include <string>

namespace midbrain::foundation_pose
{
inline void checkCuda(cudaError_t status, const char * operation, const char * file, int line)
{
  if (status == cudaSuccess) {
    return;
  }
  std::ostringstream message;
  message << operation << " failed at " << file << ':' << line << ": "
          << cudaGetErrorString(status);
  throw std::runtime_error(message.str());
}
}  // namespace midbrain::foundation_pose

#define CHECK_CUDA_ERROR(status, operation) \
  ::midbrain::foundation_pose::checkCuda((status), (operation), __FILE__, __LINE__)
