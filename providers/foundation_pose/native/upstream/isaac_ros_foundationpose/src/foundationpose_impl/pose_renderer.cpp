// SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
// Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// SPDX-License-Identifier: Apache-2.0

#include "isaac_ros_foundationpose/foundationpose_impl/pose_renderer.hpp"

#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <vector>

#include "CudaRaster.hpp"
#include "Eigen/Dense"
#include "foundationpose_render.cu.hpp"
#include "isaac_ros_common/cuda_stream.hpp"
#include "midbrain_foundation_pose/windows_preprocess.hpp"

namespace nvidia
{
namespace isaac_ros
{
namespace foundationpose
{

namespace
{
constexpr size_t kVertexPts = 3;
constexpr size_t kTexcoordDim = 2;
constexpr size_t kPTMatrixDim = 3;
constexpr size_t kPoseLen = 4;
constexpr size_t kOutputRank = 4;

typedef Eigen::Matrix<float, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor> RowMajorMatrix;

RowMajorMatrix computeTF(float l, float r, float t, float b, Eigen::Vector2i out_size)
{
  l = std::round(l); r = std::round(r); t = std::round(t); b = std::round(b);
  RowMajorMatrix tf = Eigen::MatrixXf::Identity(3, 3);
  tf(0, 2) = -l; tf(1, 2) = -t;
  RowMajorMatrix s = Eigen::MatrixXf::Identity(3, 3);
  s(0, 0) = out_size(0) / (r - l);
  s(1, 1) = out_size(1) / (b - t);
  return s * tf;
}

std::vector<RowMajorMatrix> computeCropWindowTF(
  const std::vector<Eigen::MatrixXf> & poses, const Eigen::Matrix3f & K,
  Eigen::Vector2i out_size, float crop_ratio, float mesh_diameter)
{
  float rad = mesh_diameter * crop_ratio / 2.0f;
  Eigen::MatrixXf offsets(5, 3);
  offsets << 0, 0, 0, rad, 0, 0, -rad, 0, 0, 0, rad, 0, 0, -rad, 0;
  std::vector<RowMajorMatrix> tfs;
  for (const auto & pose : poses) {
    auto block = pose.block<3, 1>(0, 3).transpose();
    Eigen::MatrixXf pts = block.replicate(5, 1).array() + offsets.array();
    Eigen::MatrixXf proj = (K * pts.transpose()).transpose();
    Eigen::MatrixXf uvs = proj.leftCols(2).array() / proj.rightCols(1).replicate(1, 2).array();
    Eigen::MatrixXf center = uvs.row(0);
    float radius = (uvs - center.replicate(uvs.rows(), 1)).array().abs().maxCoeff();
    tfs.push_back(computeTF(
        center(0, 0) - radius, center(0, 0) + radius,
        center(0, 1) - radius, center(0, 1) + radius, out_size));
  }
  return tfs;
}

void projMatFromIntrinsics(
  Eigen::Matrix4f & out, const Eigen::Matrix3f & K, int h, int w,
  float znear = 0.1f, float zfar = 100.0f)
{
  float depth = zfar - znear;
  float q = -(zfar + znear) / depth;
  float qn = -2.0f * zfar * znear / depth;
  out << 2 * K(0, 0) / w, -2 * K(0, 1) / w, (-2 * K(0, 2) + w) / w, 0,
    0, 2 * K(1, 1) / h, (2 * K(1, 2) - h) / h, 0,
    0, 0, q, qn,
    0, 0, -1, 0;
}

void constructBBox2D(
  Eigen::MatrixXf & bbox2d, const std::vector<RowMajorMatrix> & tfs, int H, int W)
{
  Eigen::MatrixXf crop(2, 2);
  crop << 0.0, 0.0, W - 1, H - 1;
  for (size_t i = 0; i < tfs.size(); i++) {
    auto inv = tfs[i].inverse();
    for (int j = 0; j < 2; j++) {
      Eigen::Vector3f pt;
      pt << crop(j, 0), crop(j, 1), 1.0f;
      Eigen::Vector3f r = inv * pt;
      bbox2d(i, j * 2) = r(0);
      bbox2d(i, j * 2 + 1) = r(1);
    }
  }
}

}  // namespace

PoseRenderer::PoseRenderer(const PoseRendererParams & params, cudaStream_t stream)
: params_(params), stream_(stream)
{
}

PoseRenderer::~PoseRenderer()
{
  freeDeviceMemory();
}

void PoseRenderer::allocateDeviceMemory(
  uint32_t N, uint32_t H, uint32_t W, uint32_t C, uint32_t num_vertices)
{
  if (device_mem_cached_ && num_vertices == num_vertices_cache_ && N <= batch_size_cache_) {
    return;
  }

  freeDeviceMemory();

  CHECK_CUDA_ERROR(cudaMalloc(&rast_out_device_, N * H * W * 4 * sizeof(float)), "rast_out");
  size_t nhwc = N * H * W * C * sizeof(float);
  CHECK_CUDA_ERROR(cudaMalloc(&texcoords_out_device_,
    N * H * W * kTexcoordDim * sizeof(float)), "texcoords_out");
  CHECK_CUDA_ERROR(cudaMalloc(&diffuse_map_device_, N * H * W * sizeof(float)), "diffuse_map");
  CHECK_CUDA_ERROR(cudaMalloc(&color_device_, nhwc), "color");
  CHECK_CUDA_ERROR(cudaMalloc(&xyz_map_device_, nhwc), "xyz_map");
  CHECK_CUDA_ERROR(cudaMalloc(&flipped_color_device_, nhwc), "flipped_color");
  CHECK_CUDA_ERROR(cudaMalloc(&flipped_xyz_map_device_, nhwc), "flipped_xyz");
  CHECK_CUDA_ERROR(cudaMalloc(&bbox2d_device_, N * 4 * sizeof(float)), "bbox2d");
  CHECK_CUDA_ERROR(cudaMalloc(&transformed_xyz_map_device_, nhwc), "txyz");
  CHECK_CUDA_ERROR(cudaMalloc(&transformed_rgb_device_, nhwc), "trgb");
  CHECK_CUDA_ERROR(cudaMalloc(&wp_image_device_, N * H * W * C * sizeof(uint8_t)), "wp");
  CHECK_CUDA_ERROR(cudaMalloc(&trans_matrix_device_, N * 9 * sizeof(float)), "trans_mat");

  size_t pts_cam_sz = N * num_vertices * kVertexPts * sizeof(float);
  size_t pose_clip_sz = N * num_vertices * 4 * sizeof(float);
  CHECK_CUDA_ERROR(cudaMalloc(&pts_cam_device_, pts_cam_sz), "pts_cam");
  CHECK_CUDA_ERROR(cudaMalloc(&pose_clip_device_, pose_clip_sz), "pose_clip");
  CHECK_CUDA_ERROR(cudaMalloc(&diffuse_vertex_device_, N * num_vertices * sizeof(float)),
          "diffuse_vert");

  cr_ = new CR::CudaRaster();
  device_mem_cached_ = true;
  num_vertices_cache_ = num_vertices;
  batch_size_cache_ = N;
}

void PoseRenderer::freeDeviceMemory()
{
  if (!device_mem_cached_) {return;}
  if (cr_) {delete cr_; cr_ = nullptr;}
  auto f = [](auto & p) {if (p) {cudaFree(p); p = nullptr;}};
  f(rast_out_device_); f(texcoords_out_device_); f(color_device_);
  f(diffuse_vertex_device_); f(diffuse_map_device_);
  f(xyz_map_device_); f(flipped_color_device_); f(flipped_xyz_map_device_);
  f(bbox2d_device_); f(transformed_xyz_map_device_);
  f(transformed_rgb_device_);
  f(wp_image_device_); f(trans_matrix_device_); f(pts_cam_device_); f(pose_clip_device_);
  f(norm_tex_device_);
  norm_tex_num_verts_ = 0;
  device_mem_cached_ = false;
}

void PoseRenderer::renderRefine(
  const float * poses_device, uint32_t N,
  const float * point_cloud_device, const uint8_t * rgb_device,
  const Eigen::Matrix3f & K, uint32_t rgb_H, uint32_t rgb_W,
  std::shared_ptr<const MeshData> md,
  float * rendered_out_device,
  float * observed_out_device)
{
  uint32_t H = params_.resized_height;
  uint32_t W = params_.resized_width;
  uint32_t C = kNumChannels;

  allocateDeviceMemory(N, H, W, C, md->num_vertices);

  // Copy poses to host
  std::vector<float> ph(N * 16);
  CHECK_CUDA_ERROR(cudaMemcpyAsync(ph.data(), poses_device, ph.size() * sizeof(float),
    cudaMemcpyDeviceToHost, stream_), "poses D2H");
  CHECK_CUDA_ERROR(cudaStreamSynchronize(stream_), "sync poses");

  std::vector<Eigen::MatrixXf> poses;
  for (uint32_t i = 0; i < N; i++) {
    poses.push_back(Eigen::Map<Eigen::MatrixXf>(ph.data() + i * 16, 4, 4));
  }

  Eigen::Vector2i out_size{static_cast<int>(H), static_cast<int>(W)};
  auto tfs = computeCropWindowTF(poses, K, out_size, params_.crop_ratio, md->mesh_diameter);

  Eigen::MatrixXf bbox2d(tfs.size(), 4);
  constructBBox2D(bbox2d, tfs, H, W);

  // nvdiffrast render
  nvidia::isaac_ros::transform_pts(stream_, pts_cam_device_, md->mesh_vertices_device,
    const_cast<float *>(poses_device), md->num_vertices, kVertexPts, N, kPoseLen);
  CHECK_CUDA_ERROR(cudaGetLastError(), "transform_pts");

  Eigen::Matrix4f proj_mat;
  projMatFromIntrinsics(proj_mat, K, rgb_H, rgb_W);

  std::vector<float> bb_flat;
  for (int j = 0; j < bbox2d.rows(); j++) {
    for (int k = 0; k < bbox2d.cols(); k++) {
      bb_flat.push_back(bbox2d(j, k));
    }
  }
  CHECK_CUDA_ERROR(cudaMemcpyAsync(bbox2d_device_, bb_flat.data(), N * 4 * sizeof(float),
    cudaMemcpyHostToDevice, stream_), "bbox2d H2D");

  nvidia::isaac_ros::generate_pose_clip(stream_, pose_clip_device_,
    const_cast<float *>(poses_device), bbox2d_device_, md->mesh_vertices_device,
    proj_mat, rgb_H, rgb_W, md->num_vertices, N);
  CHECK_CUDA_ERROR(cudaGetLastError(), "pose_clip");

  nvidia::isaac_ros::rasterize(stream_, cr_, pose_clip_device_, md->mesh_faces_device,
    rast_out_device_, md->num_vertices, md->num_faces, H, W, N);
  CHECK_CUDA_ERROR(cudaGetLastError(), "rasterize");

  nvidia::isaac_ros::interpolate(stream_, pts_cam_device_, rast_out_device_,
    md->mesh_faces_device, xyz_map_device_, md->num_vertices, md->num_faces, kVertexPts, H, W, N);
  CHECK_CUDA_ERROR(cudaGetLastError(), "interpolate xyz");

  nvidia::isaac_ros::compute_vertex_diffuse(
    stream_, diffuse_vertex_device_, md->mesh_normals_device, const_cast<float *>(poses_device),
    N, md->num_vertices);
  CHECK_CUDA_ERROR(cudaGetLastError(), "compute diffuse");
  nvidia::isaac_ros::interpolate(stream_, diffuse_vertex_device_, rast_out_device_,
    md->mesh_faces_device, diffuse_map_device_, md->num_vertices, md->num_faces, 1, H, W, N);
  CHECK_CUDA_ERROR(cudaGetLastError(), "interpolate diffuse");

  // Texture - normalize texture map (cached when mesh unchanged)
  if (norm_tex_num_verts_ != md->num_vertices || !norm_tex_device_) {
    size_t tex_bytes = static_cast<size_t>(md->texture_map_height) *
      md->texture_map_width * md->texture_map_channels * sizeof(float);
    if (norm_tex_device_) {cudaFree(norm_tex_device_);}
    CHECK_CUDA_ERROR(cudaMalloc(&norm_tex_device_, tex_bytes), "norm_tex");

    midbrain::foundation_pose::convertU8ToFloat(
      stream_, md->texture_map_device, norm_tex_device_,
      tex_bytes / sizeof(float), 1.0f / 255.0f);
    norm_tex_num_verts_ = md->num_vertices;
  }

  if (md->has_tex) {
    nvidia::isaac_ros::interpolate(stream_, md->texcoords_device, rast_out_device_,
      md->mesh_faces_device, texcoords_out_device_, md->num_vertices, md->num_faces,
      kTexcoordDim, H, W, N);
    CHECK_CUDA_ERROR(cudaGetLastError(), "interpolate tex");

    nvidia::isaac_ros::texture(stream_, norm_tex_device_,
      texcoords_out_device_, color_device_,
      md->texture_map_height, md->texture_map_width, md->texture_map_channels, 1, H, W, N);
    CHECK_CUDA_ERROR(cudaGetLastError(), "texture");
  } else {
    nvidia::isaac_ros::interpolate(stream_, norm_tex_device_,
      rast_out_device_, md->mesh_faces_device, color_device_,
      md->num_vertices, md->num_faces, kVertexPts, H, W, N, 1);
    CHECK_CUDA_ERROR(cudaGetLastError(), "interpolate color");
  }

  nvidia::isaac_ros::apply_diffuse_lighting(
    stream_, color_device_, diffuse_map_device_, N, H * W);
  CHECK_CUDA_ERROR(cudaGetLastError(), "apply lighting");

  nvidia::isaac_ros::clamp(stream_, color_device_, 0.0f, 1.0f, N * H * W * C);

  midbrain::foundation_pose::flipVerticalFloat(
    stream_, color_device_, flipped_color_device_, N, H, W, C);
  midbrain::foundation_pose::flipVerticalFloat(
    stream_, xyz_map_device_, flipped_xyz_map_device_, N, H, W, C);

  // Warp observed RGB
  std::vector<float> tm_flat(N * 9, 0);
  for (uint32_t i = 0; i < N; i++) {
    for (size_t r = 0; r < kPTMatrixDim; r++) {
      for (size_t c = 0; c < kPTMatrixDim; c++) {
        tm_flat[i * 9 + r * 3 + c] = tfs[i].inverse()(r, c);
      }
    }
  }
  CHECK_CUDA_ERROR(cudaMemcpyAsync(trans_matrix_device_, tm_flat.data(),
    tm_flat.size() * sizeof(float), cudaMemcpyHostToDevice, stream_), "trans_mat H2D");

  midbrain::foundation_pose::warpPerspectiveU8(
    stream_, rgb_device, rgb_H, rgb_W, wp_image_device_, H, W, C,
    trans_matrix_device_, N);
  midbrain::foundation_pose::warpPerspectiveFloatNearest(
    stream_, point_cloud_device, rgb_H, rgb_W, transformed_xyz_map_device_,
    H, W, C, trans_matrix_device_, N);
  midbrain::foundation_pose::convertU8ToFloat(
    stream_, wp_image_device_, transformed_rgb_device_,
    static_cast<std::size_t>(N) * H * W * C, 1.0f / 255.0f);

  // Threshold and downscale point clouds
  nvidia::isaac_ros::threshold_and_downscale_pointcloud(
    stream_, transformed_xyz_map_device_, const_cast<float *>(poses_device),
    N, W * H, md->mesh_diameter / 2.0f, params_.min_depth, params_.max_depth);

  nvidia::isaac_ros::threshold_and_downscale_pointcloud(
    stream_, flipped_xyz_map_device_, const_cast<float *>(poses_device),
    N, W * H, md->mesh_diameter / 2.0f, params_.min_depth, params_.max_depth);

  // Concat color (RGB) and xyz_map (XYZ) into the caller-provided rendered
  // output buffer, and warped RGB + transformed XYZ into the observed buffer.
  nvidia::isaac_ros::concat(stream_,
    flipped_color_device_, flipped_xyz_map_device_,
    rendered_out_device, N, H, W, C, C);

  nvidia::isaac_ros::concat(stream_,
    transformed_rgb_device_, transformed_xyz_map_device_,
    observed_out_device, N, H, W, C, C);

  // No cudaStreamSynchronize here: the caller's NitrosTensor WriteHandle dtor
  // records the completion event on stream_ AFTER our queued kernels.
}

}  // namespace foundationpose
}  // namespace isaac_ros
}  // namespace nvidia
