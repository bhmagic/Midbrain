#include "midbrain_foundation_pose/native_api.h"

#include <cuda_runtime.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "Eigen/Dense"
#include "foundationpose_sampling.cu.hpp"
#include "isaac_ros_common/cuda_stream.hpp"
#include "isaac_ros_foundationpose/foundationpose_impl/mesh_loader.hpp"
#include "isaac_ros_foundationpose/foundationpose_impl/pose_renderer.hpp"
#include "isaac_ros_foundationpose/foundationpose_impl/pose_sampler.hpp"
#include "isaac_ros_foundationpose/foundationpose_impl/pose_transformer.hpp"

namespace midbrain::foundation_pose
{
namespace upstream = nvidia::isaac_ros::foundationpose;

namespace
{
void copyError(const std::string & message, char * output, std::size_t capacity)
{
  if (!output || capacity == 0) {
    return;
  }
  const std::size_t length = std::min(capacity - 1, message.size());
  std::memcpy(output, message.data(), length);
  output[length] = '\0';
}

template<typename T>
void allocateDevice(T ** pointer, std::size_t count, const char * label)
{
  CHECK_CUDA_ERROR(cudaMalloc(pointer, count * sizeof(T)), label);
}

template<typename T>
void releaseDevice(T *& pointer)
{
  if (pointer) {
    cudaFree(pointer);
    pointer = nullptr;
  }
}

class NativePipeline
{
public:
  explicit NativePipeline(const MidbrainFoundationPoseCreateConfig & config)
  : config_(config)
  {
    if (!config_.inference_callback) {
      throw std::runtime_error("TensorRT inference callback is required");
    }
    if (config_.max_hypotheses == 0 || config_.max_hypotheses % 6 != 0) {
      throw std::runtime_error("max_hypotheses must be positive and divisible by six");
    }
    if (config_.resized_height == 0 || config_.resized_width == 0) {
      throw std::runtime_error("network input dimensions must be positive");
    }
    CHECK_CUDA_ERROR(cudaStreamCreate(&stream_), "create FoundationPose CUDA stream");

    mesh_loader_ = std::make_unique<upstream::MeshLoader>(stream_);
    upstream::PoseSamplerParams sampler_params;
    sampler_params.max_hypothesis = config_.max_hypotheses;
    sampler_params.min_depth = config_.min_depth_m;
    sampler_ = std::make_unique<upstream::PoseSampler>(sampler_params, stream_);

    upstream::PoseRendererParams refine_params;
    refine_params.crop_ratio = config_.refine_crop_ratio;
    refine_params.min_depth = config_.min_depth_m;
    refine_params.max_depth = config_.max_depth_m;
    refine_params.resized_height = config_.resized_height;
    refine_params.resized_width = config_.resized_width;
    refine_renderer_ = std::make_unique<upstream::PoseRenderer>(refine_params, stream_);
    upstream::PoseRendererParams score_params = refine_params;
    score_params.crop_ratio = config_.score_crop_ratio;
    score_renderer_ = std::make_unique<upstream::PoseRenderer>(score_params, stream_);
    transformer_ = std::make_unique<upstream::PoseTransformer>(
      config_.rotation_normalizer, stream_);

    const std::size_t max_poses = config_.max_hypotheses;
    const std::size_t refine_batch = max_poses / 6;
    const std::size_t elements_per_pose =
      static_cast<std::size_t>(config_.resized_height) * config_.resized_width * 6;
    allocateDevice(&poses_device_, max_poses * 16, "allocate pose hypotheses");
    allocateDevice(&refine_rendered_device_, refine_batch * elements_per_pose,
      "allocate refine rendered tensor");
    allocateDevice(&refine_observed_device_, refine_batch * elements_per_pose,
      "allocate refine observed tensor");
    allocateDevice(&refine_translation_device_, refine_batch * 3,
      "allocate refine translation output");
    allocateDevice(&refine_rotation_device_, refine_batch * 3,
      "allocate refine rotation output");
    allocateDevice(&score_rendered_device_, max_poses * elements_per_pose,
      "allocate score rendered tensor");
    allocateDevice(&score_observed_device_, max_poses * elements_per_pose,
      "allocate score observed tensor");
    allocateDevice(&scores_device_, max_poses, "allocate score output");
  }

  ~NativePipeline()
  {
    score_renderer_.reset();
    refine_renderer_.reset();
    transformer_.reset();
    sampler_.reset();
    mesh_loader_.reset();
    releaseFrameBuffers();
    releaseDevice(poses_device_);
    releaseDevice(refine_rendered_device_);
    releaseDevice(refine_observed_device_);
    releaseDevice(refine_translation_device_);
    releaseDevice(refine_rotation_device_);
    releaseDevice(score_rendered_device_);
    releaseDevice(score_observed_device_);
    releaseDevice(scores_device_);
    if (stream_) {
      cudaStreamDestroy(stream_);
      stream_ = nullptr;
    }
  }

  void estimate(
    const MidbrainFoundationPoseEstimateRequest & request,
    MidbrainFoundationPoseEstimateResult & result)
  {
    validate(request);
    const auto started = std::chrono::steady_clock::now();
    mesh_loader_->tryReload(request.mesh_path_utf8, request.mesh_scale_to_m);
    const auto mesh = mesh_loader_->getMeshData();
    if (!mesh || mesh->num_vertices == 0) {
      throw std::runtime_error("CAD mesh did not load");
    }
    ensureFrameBuffers(request.height, request.width);
    const std::size_t pixels =
      static_cast<std::size_t>(request.height) * request.width;
    CHECK_CUDA_ERROR(cudaMemcpyAsync(
      rgb_device_, request.rgb_host, pixels * 3, cudaMemcpyHostToDevice, stream_),
      "upload RGB");
    CHECK_CUDA_ERROR(cudaMemcpyAsync(
      depth_device_, request.depth_m_host, pixels * sizeof(float),
      cudaMemcpyHostToDevice, stream_), "upload depth");
    CHECK_CUDA_ERROR(cudaMemcpyAsync(
      mask_device_, request.mask_host, pixels, cudaMemcpyHostToDevice, stream_),
      "upload mask");

    Eigen::Matrix3f intrinsics;
    for (int row = 0; row < 3; ++row) {
      for (int column = 0; column < 3; ++column) {
        intrinsics(row, column) = request.camera_intrinsics_row_major[row * 3 + column];
      }
    }
    nvidia::isaac_ros::depth_to_xyz_map(
      stream_, depth_device_, point_cloud_device_, request.height, request.width,
      intrinsics(0, 0), intrinsics(1, 1), intrinsics(0, 2), intrinsics(1, 2));
    CHECK_CUDA_ERROR(cudaGetLastError(), "build point cloud");

    const upstream::SamplingResult sampling = sampler_->sample(
      depth_device_, mask_device_, request.height, request.width, intrinsics, mesh);
    if (sampling.total_poses <= 0) {
      throw std::runtime_error("FoundationPose sampling produced no pose hypotheses");
    }
    if (sampling.total_poses > static_cast<int>(config_.max_hypotheses)) {
      throw std::runtime_error("FoundationPose sampling exceeded configured hypothesis capacity");
    }
    CHECK_CUDA_ERROR(cudaMemcpyAsync(
      poses_device_, sampling.poses.data(), sampling.total_poses * 16 * sizeof(float),
      cudaMemcpyHostToDevice, stream_), "upload pose hypotheses");

    for (std::uint32_t iteration = 0; iteration < config_.refine_iterations; ++iteration) {
      for (int batch = 0; batch < sampling.num_batches; ++batch) {
        float * batch_poses = poses_device_ + batch * sampling.batch_size * 16;
        refine_renderer_->renderRefine(
          batch_poses, sampling.batch_size, point_cloud_device_, rgb_device_, intrinsics,
          request.height, request.width, mesh,
          refine_rendered_device_, refine_observed_device_);
        invokeInference(
          MIDBRAIN_FP_INFERENCE_REFINE, sampling.batch_size,
          refine_rendered_device_, refine_observed_device_,
          refine_translation_device_, refine_rotation_device_);
        transformer_->applyDeltas(
          batch_poses, sampling.batch_size, refine_translation_device_,
          refine_rotation_device_, mesh);
      }
    }

    const std::size_t floats_per_batch =
      static_cast<std::size_t>(sampling.batch_size) * config_.resized_height *
      config_.resized_width * 6;
    for (int batch = 0; batch < sampling.num_batches; ++batch) {
      float * batch_poses = poses_device_ + batch * sampling.batch_size * 16;
      score_renderer_->renderRefine(
        batch_poses, sampling.batch_size, point_cloud_device_, rgb_device_, intrinsics,
        request.height, request.width, mesh,
        score_rendered_device_ + batch * floats_per_batch,
        score_observed_device_ + batch * floats_per_batch);
    }
    invokeInference(
      MIDBRAIN_FP_INFERENCE_SCORE, sampling.total_poses,
      score_rendered_device_, score_observed_device_, scores_device_, nullptr);

    std::vector<float> scores(static_cast<std::size_t>(sampling.total_poses));
    CHECK_CUDA_ERROR(cudaMemcpyAsync(
      scores.data(), scores_device_, scores.size() * sizeof(float),
      cudaMemcpyDeviceToHost, stream_), "download scores");
    CHECK_CUDA_ERROR(cudaStreamSynchronize(stream_), "synchronize FoundationPose estimate");
    const auto best = std::max_element(scores.begin(), scores.end());
    const std::size_t best_index = static_cast<std::size_t>(best - scores.begin());
    CHECK_CUDA_ERROR(cudaMemcpy(
      result.camera_from_centered_mesh_column_major,
      poses_device_ + best_index * 16, 16 * sizeof(float), cudaMemcpyDeviceToHost),
      "download selected pose");
    result.score = *best;
    result.hypothesis_count = static_cast<std::uint32_t>(sampling.total_poses);
    result.elapsed_ms = std::chrono::duration<float, std::milli>(
      std::chrono::steady_clock::now() - started).count();
  }

private:
  void validate(const MidbrainFoundationPoseEstimateRequest & request) const
  {
    if (request.struct_size != sizeof(MidbrainFoundationPoseEstimateRequest)) {
      throw std::runtime_error("estimate request ABI size mismatch");
    }
    if (!request.rgb_host || !request.depth_m_host || !request.mask_host) {
      throw std::runtime_error("RGB, depth, and mask host pointers are required");
    }
    if (!request.mesh_path_utf8 || request.mesh_path_utf8[0] == '\0') {
      throw std::runtime_error("mesh path is required");
    }
    if (request.height == 0 || request.width == 0) {
      throw std::runtime_error("image dimensions must be positive");
    }
    if (!std::isfinite(request.mesh_scale_to_m) || request.mesh_scale_to_m <= 0.0f) {
      throw std::runtime_error("mesh scale must be finite and positive");
    }
    for (float value : request.camera_intrinsics_row_major) {
      if (!std::isfinite(value)) {
        throw std::runtime_error("camera intrinsics contain a non-finite value");
      }
    }
  }

  void ensureFrameBuffers(std::uint32_t height, std::uint32_t width)
  {
    if (height == frame_height_ && width == frame_width_) {
      return;
    }
    releaseFrameBuffers();
    const std::size_t pixels = static_cast<std::size_t>(height) * width;
    allocateDevice(&rgb_device_, pixels * 3, "allocate RGB frame");
    allocateDevice(&depth_device_, pixels, "allocate depth frame");
    allocateDevice(&mask_device_, pixels, "allocate mask frame");
    allocateDevice(&point_cloud_device_, pixels * 3, "allocate point cloud");
    frame_height_ = height;
    frame_width_ = width;
  }

  void releaseFrameBuffers()
  {
    releaseDevice(rgb_device_);
    releaseDevice(depth_device_);
    releaseDevice(mask_device_);
    releaseDevice(point_cloud_device_);
    frame_height_ = 0;
    frame_width_ = 0;
  }

  void invokeInference(
    int kind, int batch, float * rendered, float * observed,
    float * primary, float * secondary)
  {
    char error[1024] = {};
    const int status = config_.inference_callback(
      config_.inference_user_data, kind, batch,
      static_cast<int>(config_.resized_height),
      static_cast<int>(config_.resized_width), 6,
      reinterpret_cast<std::uint64_t>(rendered),
      reinterpret_cast<std::uint64_t>(observed),
      reinterpret_cast<std::uint64_t>(primary),
      reinterpret_cast<std::uint64_t>(secondary),
      reinterpret_cast<std::uint64_t>(stream_), error, sizeof(error));
    if (status != 0) {
      throw std::runtime_error(
              std::string("TensorRT ") +
              (kind == MIDBRAIN_FP_INFERENCE_REFINE ? "refine" : "score") +
              " callback failed: " + (error[0] ? error : "unknown error"));
    }
  }

  MidbrainFoundationPoseCreateConfig config_{};
  cudaStream_t stream_{nullptr};
  std::unique_ptr<upstream::MeshLoader> mesh_loader_;
  std::unique_ptr<upstream::PoseSampler> sampler_;
  std::unique_ptr<upstream::PoseRenderer> refine_renderer_;
  std::unique_ptr<upstream::PoseRenderer> score_renderer_;
  std::unique_ptr<upstream::PoseTransformer> transformer_;

  std::uint8_t * rgb_device_{nullptr};
  float * depth_device_{nullptr};
  std::uint8_t * mask_device_{nullptr};
  float * point_cloud_device_{nullptr};
  std::uint32_t frame_height_{0};
  std::uint32_t frame_width_{0};

  float * poses_device_{nullptr};
  float * refine_rendered_device_{nullptr};
  float * refine_observed_device_{nullptr};
  float * refine_translation_device_{nullptr};
  float * refine_rotation_device_{nullptr};
  float * score_rendered_device_{nullptr};
  float * score_observed_device_{nullptr};
  float * scores_device_{nullptr};
};
}  // namespace
}  // namespace midbrain::foundation_pose

extern "C"
{
const char * MIDBRAIN_FP_CALL midbrain_foundation_pose_version()
{
  return "1.0.0-native-windows";
}

void * MIDBRAIN_FP_CALL midbrain_foundation_pose_create(
  const MidbrainFoundationPoseCreateConfig * config,
  char * error_message,
  std::size_t error_message_capacity)
{
  try {
    if (!config || config->struct_size != sizeof(MidbrainFoundationPoseCreateConfig)) {
      throw std::runtime_error("create config ABI size mismatch");
    }
    return new midbrain::foundation_pose::NativePipeline(*config);
  } catch (const std::exception & error) {
    midbrain::foundation_pose::copyError(error.what(), error_message, error_message_capacity);
    return nullptr;
  }
}

int MIDBRAIN_FP_CALL midbrain_foundation_pose_estimate(
  void * handle,
  const MidbrainFoundationPoseEstimateRequest * request,
  MidbrainFoundationPoseEstimateResult * result,
  char * error_message,
  std::size_t error_message_capacity)
{
  try {
    if (!handle || !request || !result) {
      throw std::runtime_error("estimate requires a valid handle, request, and result");
    }
    if (result->struct_size != sizeof(MidbrainFoundationPoseEstimateResult)) {
      throw std::runtime_error("estimate result ABI size mismatch");
    }
    static_cast<midbrain::foundation_pose::NativePipeline *>(handle)->estimate(
      *request, *result);
    return 0;
  } catch (const std::exception & error) {
    midbrain::foundation_pose::copyError(error.what(), error_message, error_message_capacity);
    return 1;
  }
}

void MIDBRAIN_FP_CALL midbrain_foundation_pose_destroy(void * handle)
{
  delete static_cast<midbrain::foundation_pose::NativePipeline *>(handle);
}
}
