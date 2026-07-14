#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <onnxruntime_cxx_api.h>

#include "light_ocr/core.hpp"

namespace light_ocr::internal {

enum class ModelKind { detection, recognition };

class TensorOutput {
 public:
  TensorOutput(TensorOutput&&) noexcept = default;
  TensorOutput& operator=(TensorOutput&&) noexcept = default;
  TensorOutput(const TensorOutput&) = delete;
  TensorOutput& operator=(const TensorOutput&) = delete;

  const float* data() const noexcept { return data_; }
  std::size_t size() const noexcept { return size_; }
  const std::vector<std::int64_t>& shape() const noexcept { return shape_; }

 private:
  friend class OnnxSession;

  TensorOutput(Ort::Value value, std::vector<std::int64_t> shape,
               std::size_t size);

  Ort::Value value_;
  const float* data_ = nullptr;
  std::vector<std::int64_t> shape_;
  std::size_t size_ = 0;
};

class OnnxSession {
 public:
  static Result<std::unique_ptr<OnnxSession>> create(
      const SharedBytes& model, std::uint32_t intra_op_threads,
      std::uint32_t inter_op_threads, ModelKind kind,
      std::size_t expected_recognition_classes = 0);

  Result<TensorOutput> run(const std::vector<float>& values,
                           const std::vector<std::int64_t>& shape) noexcept;

  const std::string& input_name() const noexcept { return input_name_; }
  const std::string& output_name() const noexcept { return output_name_; }

 private:
  OnnxSession(std::unique_ptr<Ort::Session> session, std::string input_name,
              std::string output_name);

  std::unique_ptr<Ort::Session> session_;
  std::string input_name_;
  std::string output_name_;
};

}  // namespace light_ocr::internal
