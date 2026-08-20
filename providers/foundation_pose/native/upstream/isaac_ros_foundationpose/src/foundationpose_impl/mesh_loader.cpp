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

#include "isaac_ros_foundationpose/foundationpose_impl/mesh_loader.hpp"

#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "isaac_ros_common/cuda_stream.hpp"

namespace nvidia::isaac_ros::foundationpose
{
namespace
{
constexpr std::uint8_t kNeutralCadColor = 128;

int parseVertexIndex(const std::string & token, std::size_t vertex_count)
{
  const std::size_t separator = token.find('/');
  const std::string value = token.substr(0, separator);
  if (value.empty()) {
    throw std::runtime_error("[MeshLoader] OBJ face contains an empty vertex index");
  }
  const long parsed = std::stol(value);
  const long resolved = parsed > 0 ? parsed - 1 : static_cast<long>(vertex_count) + parsed;
  if (resolved < 0 || resolved >= static_cast<long>(vertex_count)) {
    throw std::runtime_error("[MeshLoader] OBJ face vertex index is out of range");
  }
  return static_cast<int>(resolved);
}

Eigen::Vector3f readVertex(std::istringstream & line, float scale)
{
  Eigen::Vector3f vertex;
  if (!(line >> vertex.x() >> vertex.y() >> vertex.z())) {
    throw std::runtime_error("[MeshLoader] malformed OBJ vertex");
  }
  return vertex * scale;
}
}  // namespace

void MeshData::freeMeshDeviceMemory()
{
  if (mesh_vertices_device) {cudaFree(mesh_vertices_device);}
  if (mesh_normals_device) {cudaFree(mesh_normals_device);}
  if (mesh_faces_device) {cudaFree(mesh_faces_device);}
  if (texcoords_device) {cudaFree(texcoords_device);}
  mesh_vertices_device = nullptr;
  mesh_normals_device = nullptr;
  mesh_faces_device = nullptr;
  texcoords_device = nullptr;
}

void MeshData::freeTextureDeviceMemory()
{
  if (texture_map_device) {cudaFree(texture_map_device);}
  texture_map_device = nullptr;
}

MeshData::~MeshData()
{
  freeMeshDeviceMemory();
  freeTextureDeviceMemory();
}

MeshLoader::MeshLoader(cudaStream_t stream)
: stream_(stream), mesh_data_(std::make_shared<MeshData>())
{
}

MeshLoader::~MeshLoader()
{
  mesh_data_.reset();
}

void MeshLoader::load(const std::string & mesh_file_path, float mesh_scale)
{
  if (!std::isfinite(mesh_scale) || mesh_scale <= 0.0f) {
    throw std::runtime_error("[MeshLoader] mesh scale must be finite and positive");
  }
  requested_mesh_scale_ = mesh_scale;
  loadMeshData(mesh_file_path);
}

void MeshLoader::tryReload(const std::string & mesh_file_path, float mesh_scale)
{
  if (mesh_data_->mesh_file_path != mesh_file_path ||
    std::abs(mesh_data_->mesh_scale - mesh_scale) > 1.0e-12f)
  {
    load(mesh_file_path, mesh_scale);
  }
}

void MeshLoader::loadMeshData(const std::string & mesh_file_path)
{
  const std::filesystem::path path(mesh_file_path);
  if (!std::filesystem::is_regular_file(path)) {
    throw std::runtime_error("[MeshLoader] mesh file does not exist: " + mesh_file_path);
  }
  if (path.extension() != ".obj" && path.extension() != ".OBJ") {
    throw std::runtime_error("[MeshLoader] native Provider accepts triangulatable OBJ CAD only");
  }

  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("[MeshLoader] could not open OBJ: " + mesh_file_path);
  }

  std::vector<Eigen::Vector3f> vertices;
  std::vector<std::int32_t> faces;
  std::string raw_line;
  std::size_t line_number = 0;
  while (std::getline(input, raw_line)) {
    ++line_number;
    if (raw_line.size() < 2 || raw_line[0] == '#') {
      continue;
    }
    std::istringstream line(raw_line);
    std::string kind;
    line >> kind;
    if (kind == "v") {
      vertices.push_back(readVertex(line, requested_mesh_scale_));
    } else if (kind == "f") {
      std::vector<int> polygon;
      std::string token;
      while (line >> token) {
        polygon.push_back(parseVertexIndex(token, vertices.size()));
      }
      if (polygon.size() < 3) {
        throw std::runtime_error(
                "[MeshLoader] OBJ face has fewer than three vertices at line " +
                std::to_string(line_number));
      }
      for (std::size_t index = 1; index + 1 < polygon.size(); ++index) {
        faces.push_back(polygon[0]);
        faces.push_back(polygon[index]);
        faces.push_back(polygon[index + 1]);
      }
    }
  }
  if (vertices.empty() || faces.empty()) {
    throw std::runtime_error("[MeshLoader] OBJ has no usable triangles");
  }

  Eigen::Vector3f minimum = vertices.front();
  Eigen::Vector3f maximum = vertices.front();
  for (const auto & vertex : vertices) {
    minimum = minimum.cwiseMin(vertex);
    maximum = maximum.cwiseMax(vertex);
  }
  const Eigen::Vector3f center = (minimum + maximum) * 0.5f;
  for (auto & vertex : vertices) {
    vertex -= center;
  }

  std::vector<Eigen::Vector3f> normals(vertices.size(), Eigen::Vector3f::Zero());
  for (std::size_t face = 0; face < faces.size(); face += 3) {
    const auto a = static_cast<std::size_t>(faces[face]);
    const auto b = static_cast<std::size_t>(faces[face + 1]);
    const auto c = static_cast<std::size_t>(faces[face + 2]);
    const Eigen::Vector3f normal =
      (vertices[b] - vertices[a]).cross(vertices[c] - vertices[a]);
    if (normal.squaredNorm() > 1.0e-18f) {
      normals[a] += normal;
      normals[b] += normal;
      normals[c] += normal;
    }
  }
  for (auto & normal : normals) {
    if (normal.squaredNorm() > 1.0e-18f) {
      normal.normalize();
    } else {
      normal = Eigen::Vector3f(0.0f, 0.0f, 1.0f);
    }
  }

  std::vector<float> vertex_values;
  std::vector<float> normal_values;
  vertex_values.reserve(vertices.size() * 3);
  normal_values.reserve(normals.size() * 3);
  for (const auto & vertex : vertices) {
    vertex_values.insert(vertex_values.end(), {vertex.x(), vertex.y(), vertex.z()});
  }
  for (const auto & normal : normals) {
    normal_values.insert(normal_values.end(), {normal.x(), normal.y(), normal.z()});
  }
  std::vector<std::uint8_t> neutral_colors(vertices.size() * 3, kNeutralCadColor);

  if (mesh_data_->mesh_vertices_device) {
    mesh_data_->freeMeshDeviceMemory();
  }
  mesh_data_->freeTextureDeviceMemory();
  mesh_data_->mesh_file_path = mesh_file_path;
  mesh_data_->mesh_scale = requested_mesh_scale_;
  mesh_data_->mesh_model_center = center;
  mesh_data_->min_vertex = minimum;
  mesh_data_->max_vertex = maximum;
  mesh_data_->mesh_diameter = (maximum - minimum).norm();
  mesh_data_->num_vertices = static_cast<int>(vertices.size());
  mesh_data_->num_faces = static_cast<int>(faces.size() / 3);
  mesh_data_->num_texcoords = 0;
  mesh_data_->has_tex = false;
  mesh_data_->texture_path.clear();
  mesh_data_->texture_map_height = 1;
  mesh_data_->texture_map_width = mesh_data_->num_vertices;
  mesh_data_->texture_map_channels = 3;
  mesh_data_->mesh_vertices = Eigen::Map<
    Eigen::Matrix<float, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor>>(
    vertex_values.data(), mesh_data_->num_vertices, 3);

  CHECK_CUDA_ERROR(
    cudaMalloc(&mesh_data_->mesh_vertices_device, vertex_values.size() * sizeof(float)),
    "malloc mesh vertices");
  CHECK_CUDA_ERROR(
    cudaMalloc(&mesh_data_->mesh_normals_device, normal_values.size() * sizeof(float)),
    "malloc mesh normals");
  CHECK_CUDA_ERROR(
    cudaMalloc(&mesh_data_->mesh_faces_device, faces.size() * sizeof(std::int32_t)),
    "malloc mesh faces");
  CHECK_CUDA_ERROR(
    cudaMalloc(&mesh_data_->texture_map_device, neutral_colors.size()),
    "malloc neutral CAD colors");
  CHECK_CUDA_ERROR(
    cudaMemcpyAsync(
      mesh_data_->mesh_vertices_device, vertex_values.data(),
      vertex_values.size() * sizeof(float), cudaMemcpyHostToDevice, stream_),
    "upload mesh vertices");
  CHECK_CUDA_ERROR(
    cudaMemcpyAsync(
      mesh_data_->mesh_normals_device, normal_values.data(),
      normal_values.size() * sizeof(float), cudaMemcpyHostToDevice, stream_),
    "upload mesh normals");
  CHECK_CUDA_ERROR(
    cudaMemcpyAsync(
      mesh_data_->mesh_faces_device, faces.data(), faces.size() * sizeof(std::int32_t),
      cudaMemcpyHostToDevice, stream_),
    "upload mesh faces");
  CHECK_CUDA_ERROR(
    cudaMemcpyAsync(
      mesh_data_->texture_map_device, neutral_colors.data(), neutral_colors.size(),
      cudaMemcpyHostToDevice, stream_),
    "upload neutral CAD colors");
  CHECK_CUDA_ERROR(cudaStreamSynchronize(stream_), "synchronize mesh upload");
}
}  // namespace nvidia::isaac_ros::foundationpose
