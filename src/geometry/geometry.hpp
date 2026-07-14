#pragma once

#include <cstdint>
#include <vector>

#include <opencv2/core.hpp>

#include "light_ocr/error.hpp"
#include "light_ocr/types.hpp"
#include "model/bundle_data.hpp"

namespace light_ocr::internal {

struct TextRegionShape {
  std::uint32_t width = 0;
  std::uint32_t height = 0;
  bool rotate_counterclockwise_90 = false;

  std::uint32_t output_width() const noexcept {
    return rotate_counterclockwise_90 ? height : width;
  }

  std::uint32_t output_height() const noexcept {
    return rotate_counterclockwise_90 ? width : height;
  }
};

Quad order_quad(const cv::Point2f points[4]);
std::vector<Quad> sort_reading_order(std::vector<Quad> boxes,
                                     const GeometryConfig& config);
Result<TextRegionShape> measure_text_region(const Quad& box,
                                            const GeometryConfig& config);
Result<std::vector<cv::Mat>> crop_text_regions(const cv::Mat& bgr,
                                               const std::vector<Quad>& boxes,
                                               const GeometryConfig& config,
                                               const ResourceLimits& limits);

}  // namespace light_ocr::internal
