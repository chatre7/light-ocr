#include <cstdint>
#include <exception>
#include <fstream>
#include <iostream>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>
#include <opencv2/core.hpp>
#include <opencv2/core/utils/logger.hpp>
#include <opencv2/imgproc.hpp>

#include "common/arguments.hpp"
#include "common/bundle_files.hpp"
#include "common/process_memory.hpp"
#include "light_ocr/core.hpp"
#include "preprocess/image.hpp"

namespace {

const char* strategy_name(light_ocr::DetectionStrategy strategy) {
  return strategy == light_ocr::DetectionStrategy::bounded ? "bounded"
                                                            : "upstreamExact";
}

}  // namespace

int main(int argc, char** argv) {
  try {
    cv::utils::logging::setLogLevel(cv::utils::logging::LOG_LEVEL_SILENT);
    const auto arguments = light_ocr::tools::parse_arguments(argc, argv, true);
    if (arguments.target_width == 0 || arguments.target_height == 0 ||
        arguments.maximum_peak_bytes == 0) {
      throw std::runtime_error(
          "memory gate requires --target-width, --target-height, and "
          "--maximum-peak-bytes");
    }

    auto pixels = light_ocr::tools::read_binary_file(arguments.pixels);
    const light_ocr::ImageView source_view{
        pixels.data(), pixels.size(), arguments.width, arguments.height,
        arguments.stride, arguments.format};
    auto source_result = light_ocr::internal::validate_and_convert_image(
        source_view, light_ocr::ResourceLimits{});
    if (!source_result) {
      throw std::runtime_error(source_result.error().message + ": " +
                               source_result.error().detail);
    }
    auto source = std::move(source_result).value();
    cv::Mat target;
    cv::resize(source.bgr, target,
               cv::Size(static_cast<int>(arguments.target_width),
                        static_cast<int>(arguments.target_height)),
               0, 0, cv::INTER_LINEAR);
    source.bgr.release();
    std::vector<std::uint8_t>().swap(pixels);

    auto bundle = light_ocr::ModelBundle::create(
        light_ocr::tools::load_bundle_directory(arguments.bundle));
    if (!bundle) {
      throw std::runtime_error(bundle.error().message + ": " +
                               bundle.error().detail);
    }
    auto engine = light_ocr::Engine::create(std::move(bundle).value());
    if (!engine) {
      throw std::runtime_error(engine.error().message + ": " +
                               engine.error().detail);
    }
    const auto info = engine.value()->info();
    const light_ocr::ImageView target_view{
        target.data, target.total() * target.elemSize(), arguments.target_width,
        arguments.target_height, target.step, light_ocr::PixelFormat::bgr8};
    light_ocr::RecognizeOptions recognize_options;
    recognize_options.include_diagnostics = true;
    auto result = engine.value()->recognize(target_view, recognize_options);
    if (!result) {
      throw std::runtime_error(result.error().message + ": " +
                               result.error().detail);
    }
    if (!result.value().diagnostics) {
      throw std::runtime_error("memory gate did not receive diagnostics");
    }
    const auto& diagnostics = *result.value().diagnostics;
    const auto peak = light_ocr::tools::peak_resident_memory_bytes();
    const auto boxes_passed =
        diagnostics.accepted_boxes >= arguments.minimum_boxes &&
        (!arguments.maximum_boxes ||
         diagnostics.accepted_boxes <= *arguments.maximum_boxes);
    const auto runtime_passed =
        info.detection_strategy == light_ocr::DetectionStrategy::bounded &&
        info.detection_max_side == 960 &&
        info.default_recognition_batch_size == 1;
    const auto detection_shape_passed =
        diagnostics.detection_input_width == 960 &&
        diagnostics.detection_input_height == 960;
    bool batch_shapes_passed = true;
    nlohmann::json batch_shapes = nlohmann::json::array();
    for (const auto& shape : diagnostics.recognition_batch_shapes) {
      batch_shapes.push_back({shape.batch_size, 3, shape.height, shape.width});
      batch_shapes_passed = batch_shapes_passed && shape.batch_size == 1;
    }
    const auto peak_passed = peak <= arguments.maximum_peak_bytes;
    const auto passed = boxes_passed && runtime_passed &&
                        detection_shape_passed && batch_shapes_passed &&
                        peak_passed;
    nlohmann::json report = {
        {"schemaVersion", "1.0"},
        {"passed", passed},
        {"modelBundleId", info.model_bundle_id},
        {"image", {{"width", arguments.target_width},
                   {"height", arguments.target_height}}},
        {"runtime", {{"detectionStrategy", strategy_name(info.detection_strategy)},
                     {"detectionMaxSide", info.detection_max_side},
                     {"recognitionBatchSize", info.default_recognition_batch_size}}},
        {"result", {{"acceptedBoxes", diagnostics.accepted_boxes},
                    {"acceptedLines", result.value().lines.size()},
                    {"detectionInputShape",
                     {1, 3, diagnostics.detection_input_height,
                      diagnostics.detection_input_width}},
                    {"recognitionBatchShapes", std::move(batch_shapes)}}},
        {"memoryBytes", {{"peakResident", peak},
                         {"maximumPeakResident", arguments.maximum_peak_bytes}}},
        {"gates", {{"peakResident", peak_passed},
                   {"acceptedBoxes", boxes_passed},
                   {"runtimeDefaults", runtime_passed},
                   {"detectionInputShape", detection_shape_passed},
                   {"recognitionBatchShapes", batch_shapes_passed}}},
    };
    if (!arguments.report.empty()) {
      if (!arguments.report.parent_path().empty()) {
        std::filesystem::create_directories(arguments.report.parent_path());
      }
      std::ofstream output(arguments.report);
      if (!output) throw std::runtime_error("cannot write memory gate report");
      output << report.dump() << '\n';
    }
    std::cout << report.dump() << '\n';
    engine.value()->close();
    return passed ? 0 : 1;
  } catch (const std::exception& exception) {
    std::cout << nlohmann::json({{"schemaVersion", "1.0"}, {"passed", false},
                                 {"error", exception.what()}})
                     .dump()
              << '\n';
    return 2;
  }
}
