#pragma once

#include <cstddef>
#include <cstdint>

#if defined(_WIN32)
#if defined(MIDBRAIN_FOUNDATION_POSE_EXPORTS)
#define MIDBRAIN_FP_API __declspec(dllexport)
#else
#define MIDBRAIN_FP_API __declspec(dllimport)
#endif
#define MIDBRAIN_FP_CALL __cdecl
#else
#define MIDBRAIN_FP_API
#define MIDBRAIN_FP_CALL
#endif

extern "C"
{
enum MidbrainFoundationPoseInferenceKind
{
  MIDBRAIN_FP_INFERENCE_REFINE = 1,
  MIDBRAIN_FP_INFERENCE_SCORE = 2,
};

typedef int(MIDBRAIN_FP_CALL * MidbrainFoundationPoseInferenceCallback)(
  void * user_data,
  int inference_kind,
  int batch,
  int height,
  int width,
  int channels,
  std::uint64_t input_rendered_device,
  std::uint64_t input_observed_device,
  std::uint64_t output_primary_device,
  std::uint64_t output_secondary_device,
  std::uint64_t cuda_stream,
  char * error_message,
  std::size_t error_message_capacity);

struct MidbrainFoundationPoseCreateConfig
{
  std::uint32_t struct_size;
  std::uint32_t max_hypotheses;
  std::uint32_t refine_iterations;
  std::uint32_t resized_height;
  std::uint32_t resized_width;
  float min_depth_m;
  float max_depth_m;
  float refine_crop_ratio;
  float score_crop_ratio;
  float rotation_normalizer;
  MidbrainFoundationPoseInferenceCallback inference_callback;
  void * inference_user_data;
};

struct MidbrainFoundationPoseEstimateRequest
{
  std::uint32_t struct_size;
  const std::uint8_t * rgb_host;
  const float * depth_m_host;
  const std::uint8_t * mask_host;
  std::uint32_t height;
  std::uint32_t width;
  float camera_intrinsics_row_major[9];
  const char * mesh_path_utf8;
  float mesh_scale_to_m;
};

struct MidbrainFoundationPoseEstimateResult
{
  std::uint32_t struct_size;
  float camera_from_centered_mesh_column_major[16];
  float score;
  std::uint32_t hypothesis_count;
  float elapsed_ms;
};

MIDBRAIN_FP_API const char * MIDBRAIN_FP_CALL midbrain_foundation_pose_version();

MIDBRAIN_FP_API void * MIDBRAIN_FP_CALL midbrain_foundation_pose_create(
  const MidbrainFoundationPoseCreateConfig * config,
  char * error_message,
  std::size_t error_message_capacity);

MIDBRAIN_FP_API int MIDBRAIN_FP_CALL midbrain_foundation_pose_estimate(
  void * handle,
  const MidbrainFoundationPoseEstimateRequest * request,
  MidbrainFoundationPoseEstimateResult * result,
  char * error_message,
  std::size_t error_message_capacity);

MIDBRAIN_FP_API void MIDBRAIN_FP_CALL midbrain_foundation_pose_destroy(void * handle);
}
