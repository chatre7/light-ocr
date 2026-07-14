#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string_view>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "common/arguments.hpp"
#include "common/bundle_files.hpp"
#include "common/process_memory.hpp"
#include "light_ocr/core.hpp"
#include "util/sha256.hpp"

namespace {

using Samples = std::vector<std::uint64_t>;

nlohmann::json distribution(Samples values) {
  std::sort(values.begin(), values.end());
  const auto percentile = [&values](double value) {
    const auto index = static_cast<std::size_t>(
        std::min<double>(values.size() - 1, std::ceil(value * values.size()) - 1));
    return values[index];
  };
  return {{"minimum", values.front()}, {"median", percentile(0.5)},
          {"p95", percentile(0.95)}, {"maximum", values.back()}};
}

std::uint64_t elapsed_us(std::chrono::steady_clock::time_point begin,
                         std::chrono::steady_clock::time_point end) {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::microseconds>(end - begin).count());
}

std::string stable_result_hash(const light_ocr::OcrResult& result) {
  auto lines = nlohmann::json::array();
  for (const auto& line : result.lines) {
    auto points = nlohmann::json::array();
    for (const auto& point : line.box.points) {
      points.push_back({point.x, point.y});
    }
    lines.push_back({{"text", line.text}, {"confidence", line.confidence},
                     {"box", std::move(points)}});
  }
  const auto canonical = nlohmann::json({{"modelBundleId", result.model_bundle_id},
                                          {"lines", std::move(lines)}})
                             .dump();
  return light_ocr::internal::sha256_hex(
      reinterpret_cast<const std::uint8_t*>(canonical.data()), canonical.size());
}

}  // namespace

int main(int argc, char** argv) {
  try {
    const auto arguments = light_ocr::tools::parse_arguments(argc, argv, true);
    const auto load_begin = std::chrono::steady_clock::now();
    auto files = light_ocr::tools::load_bundle_directory(arguments.bundle);
    std::uint64_t model_bundle_bytes = 0;
    for (const auto& file : files) model_bundle_bytes += file.bytes->size();
    auto bundle = light_ocr::ModelBundle::create(std::move(files));
    if (!bundle) throw std::runtime_error(bundle.error().message + ": " + bundle.error().detail);
    const auto load_end = std::chrono::steady_clock::now();

    const auto initialize_begin = std::chrono::steady_clock::now();
    auto engine_options = light_ocr::tools::engine_options_for_profile(arguments.profile);
    auto engine = light_ocr::Engine::create(std::move(bundle).value(), engine_options);
    if (!engine) throw std::runtime_error(engine.error().message + ": " + engine.error().detail);
    const auto initialize_end = std::chrono::steady_clock::now();

    auto pixels = light_ocr::tools::read_binary_file(arguments.pixels);
    const light_ocr::ImageView image{pixels.data(), pixels.size(), arguments.width,
                                     arguments.height, arguments.stride, arguments.format};
    for (std::uint32_t index = 0; index < arguments.warmup; ++index) {
      auto result = engine.value()->recognize(image);
      if (!result) throw std::runtime_error(result.error().message + ": " + result.error().detail);
    }

    Samples wall;
    Samples total;
    Samples inference_only;
    std::array<Samples, 9> stages;
    Samples resident;
    std::vector<std::string> result_hashes;
    wall.reserve(arguments.iterations);
    total.reserve(arguments.iterations);
    inference_only.reserve(arguments.iterations);
    resident.reserve(arguments.iterations);
    result_hashes.reserve(arguments.iterations);
    std::uint32_t accepted_boxes = 0;
    std::size_t accepted_lines = 0;
    std::uint32_t detection_input_width = 0;
    std::uint32_t detection_input_height = 0;
    std::vector<light_ocr::RecognitionBatchShape> recognition_batch_shapes;
    std::vector<light_ocr::DetectionPassShape> detection_passes;
    std::uint32_t raw_detection_boxes = 0;
    std::uint32_t suppressed_duplicate_boxes = 0;
    std::uint32_t max_live_detection_pass_buffers = 0;
    light_ocr::RecognizeOptions recognize_options;
    recognize_options.include_diagnostics = true;
    for (auto& samples : stages) samples.reserve(arguments.iterations);

    for (std::uint32_t index = 0; index < arguments.iterations; ++index) {
      const auto begin = std::chrono::steady_clock::now();
      auto result = engine.value()->recognize(image, recognize_options);
      const auto end = std::chrono::steady_clock::now();
      if (!result) throw std::runtime_error(result.error().message + ": " + result.error().detail);
      const auto& timing = result.value().timing;
      result_hashes.push_back(stable_result_hash(result.value()));
      accepted_lines = result.value().lines.size();
      if (result.value().diagnostics) {
        accepted_boxes = result.value().diagnostics->accepted_boxes;
        detection_input_width = result.value().diagnostics->detection_input_width;
        detection_input_height = result.value().diagnostics->detection_input_height;
        recognition_batch_shapes =
            result.value().diagnostics->recognition_batch_shapes;
        detection_passes = result.value().diagnostics->detection_passes;
        raw_detection_boxes = result.value().diagnostics->raw_detection_boxes;
        suppressed_duplicate_boxes =
            result.value().diagnostics->suppressed_duplicate_boxes;
        max_live_detection_pass_buffers =
            result.value().diagnostics->max_live_detection_pass_buffers;
      }
      wall.push_back(elapsed_us(begin, end));
      total.push_back(timing.total_us);
      inference_only.push_back(timing.detection_inference_us + timing.recognition_inference_us);
      stages[0].push_back(timing.input_validation_us);
      stages[1].push_back(timing.detection_preprocess_us);
      stages[2].push_back(timing.detection_inference_us);
      stages[3].push_back(timing.detection_postprocess_us);
      stages[4].push_back(timing.detection_merge_us);
      stages[5].push_back(timing.crop_and_sort_us);
      stages[6].push_back(timing.recognition_preprocess_us);
      stages[7].push_back(timing.recognition_inference_us);
      stages[8].push_back(timing.recognition_postprocess_us);
      resident.push_back(light_ocr::tools::resident_memory_bytes());
    }

    constexpr std::array<std::string_view, 9> stage_names = {
        "inputValidation", "detectionPreprocess", "detectionInference",
        "detectionPostprocess", "detectionMerge", "cropAndSort", "recognitionPreprocess",
        "recognitionInference", "recognitionPostprocess"};
    nlohmann::json stage_report = nlohmann::json::object();
    for (std::size_t index = 0; index < stages.size(); ++index) {
      stage_report[stage_names[index]] = distribution(std::move(stages[index]));
    }
    const auto resident_minmax = std::minmax_element(resident.begin(), resident.end());
    if (!std::all_of(result_hashes.begin(), result_hashes.end(),
                     [&result_hashes](const std::string& value) {
                       return value == result_hashes.front();
                     })) {
      throw std::runtime_error("benchmark result changed between measured iterations");
    }
    const auto engine_info = engine.value()->info();
    const auto model_bundle_id = engine_info.model_bundle_id;
    const auto detection_strategy = engine_info.detection_strategy ==
                                            light_ocr::DetectionStrategy::bounded
                                        ? "bounded"
                                    : engine_info.detection_strategy ==
                                              light_ocr::DetectionStrategy::tiled
                                        ? "tiled"
                                        : "upstreamExact";
    nlohmann::json recognition_shapes = nlohmann::json::array();
    for (const auto& shape : recognition_batch_shapes) {
      recognition_shapes.push_back({shape.batch_size, 3, shape.height, shape.width});
    }
    nlohmann::json pass_report = nlohmann::json::array();
    for (const auto& pass : detection_passes) {
      pass_report.push_back({{"tileOrdinal", pass.tile_ordinal},
                             {"roi", {pass.x, pass.y, pass.width, pass.height}},
                             {"tensorShape", {1, 3, pass.tensor_height, pass.tensor_width}},
                             {"contourCandidates", pass.contour_candidates},
                             {"rawCandidates", pass.raw_candidates}});
    }
    engine.value()->close();

    const auto report = nlohmann::json({
        {"schemaVersion", "1.0"}, {"ok", true}, {"backend", "native-cpp"},
        {"modelBundleId", model_bundle_id}, {"modelBundleBytes", model_bundle_bytes},
        {"profile", arguments.profile},
        {"runtime", {{"coreVersion", engine_info.core_version},
                     {"normalizedConfigSchemaVersion",
                      engine_info.normalized_config_schema_version},
                     {"detectionStrategy", detection_strategy},
                     {"detectionMaxSide", engine_info.detection_max_side},
                     {"recognitionBatchSize", engine_info.default_recognition_batch_size}}},
        {"result", {{"acceptedBoxes", accepted_boxes},
                    {"acceptedLines", accepted_lines},
                    {"stableSha256", result_hashes.front()},
                    {"rawDetectionBoxes", raw_detection_boxes},
                    {"suppressedDuplicateBoxes", suppressed_duplicate_boxes},
                    {"maxLiveDetectionPassBuffers", max_live_detection_pass_buffers},
                    {"detectionPasses", std::move(pass_report)},
                    {"detectionInputShape", {1, 3, detection_input_height,
                                              detection_input_width}},
                    {"recognitionBatchShapes", std::move(recognition_shapes)}}},
        {"loadUs", elapsed_us(load_begin, load_end)},
        {"engineInitializationUs", elapsed_us(initialize_begin, initialize_end)},
        {"warmup", arguments.warmup}, {"iterations", arguments.iterations},
        {"latencyUs", distribution(std::move(wall))},
        {"reportedTotalUs", distribution(std::move(total))},
        {"inferenceOnlyUs", distribution(std::move(inference_only))},
        {"stagesUs", std::move(stage_report)},
        {"memoryBytes", {{"residentMinimum", *resident_minmax.first},
                         {"residentMaximum", *resident_minmax.second},
                         {"residentFinal", resident.back()},
                         {"peakResident", light_ocr::tools::peak_resident_memory_bytes()}}}})
                            .dump() + "\n";
    if (!arguments.report.empty()) {
      const auto parent = arguments.report.parent_path();
      if (!parent.empty()) std::filesystem::create_directories(parent);
      std::ofstream stream(arguments.report, std::ios::binary | std::ios::trunc);
      stream.exceptions(std::ios::badbit | std::ios::failbit);
      stream << report;
    }
    std::cout << report;
    return 0;
  } catch (const std::exception& exception) {
    std::cout << nlohmann::json({{"schemaVersion", "1.0"}, {"ok", false},
                                 {"error", exception.what()}}).dump()
              << '\n';
    return 2;
  }
}
