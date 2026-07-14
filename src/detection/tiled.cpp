#include "detection/tiled.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <utility>
#include <vector>

#include "util/checked_math.hpp"

namespace light_ocr::internal {
namespace {

constexpr double kGeometryEpsilon = 1e-9;

template <class T>
Result<T> failure(ErrorCode code, const char* message) {
  return Result<T>::failure(Error{code, message, {}});
}

struct DoublePoint {
  double x = 0;
  double y = 0;
};

double cross(const DoublePoint& left, const DoublePoint& right) {
  return left.x * right.y - left.y * right.x;
}

DoublePoint subtract(const DoublePoint& left, const DoublePoint& right) {
  return DoublePoint{left.x - right.x, left.y - right.y};
}

double signed_area(const std::vector<DoublePoint>& polygon) {
  double sum = 0;
  for (std::size_t index = 0; index < polygon.size(); ++index) {
    const auto& left = polygon[index];
    const auto& right = polygon[(index + 1) % polygon.size()];
    sum += cross(left, right);
  }
  return sum / 2.0;
}

double area(const std::vector<DoublePoint>& polygon) {
  return std::abs(signed_area(polygon));
}

bool valid_convex_quad(const Quad& quad) {
  double expected_sign = 0;
  for (std::size_t index = 0; index < quad.points.size(); ++index) {
    const auto& first = quad.points[index];
    const auto& second = quad.points[(index + 1) % quad.points.size()];
    const auto& third = quad.points[(index + 2) % quad.points.size()];
    if (!std::isfinite(first.x) || !std::isfinite(first.y)) return false;
    const DoublePoint left{static_cast<double>(second.x) - first.x,
                           static_cast<double>(second.y) - first.y};
    const DoublePoint right{static_cast<double>(third.x) - second.x,
                            static_cast<double>(third.y) - second.y};
    const auto value = cross(left, right);
    if (!std::isfinite(value) || std::abs(value) <= kGeometryEpsilon) return false;
    const auto sign = value > 0 ? 1.0 : -1.0;
    if (expected_sign == 0) {
      expected_sign = sign;
    } else if (sign != expected_sign) {
      return false;
    }
  }
  std::vector<DoublePoint> polygon;
  polygon.reserve(quad.points.size());
  for (const auto& point : quad.points) polygon.push_back({point.x, point.y});
  return area(polygon) > kGeometryEpsilon;
}

std::vector<DoublePoint> to_polygon(const Quad& quad) {
  std::vector<DoublePoint> result;
  result.reserve(quad.points.size());
  for (const auto& point : quad.points) result.push_back({point.x, point.y});
  return result;
}

bool inside_clip_edge(const DoublePoint& point, const DoublePoint& edge_begin,
                      const DoublePoint& edge_end, double orientation) {
  const auto value = cross(subtract(edge_end, edge_begin),
                           subtract(point, edge_begin));
  return orientation > 0 ? value >= -kGeometryEpsilon
                         : value <= kGeometryEpsilon;
}

DoublePoint line_intersection(const DoublePoint& segment_begin,
                              const DoublePoint& segment_end,
                              const DoublePoint& edge_begin,
                              const DoublePoint& edge_end) {
  const auto segment = subtract(segment_end, segment_begin);
  const auto edge = subtract(edge_end, edge_begin);
  const auto denominator = cross(segment, edge);
  if (std::abs(denominator) <= kGeometryEpsilon) return segment_end;
  const auto ratio = cross(subtract(edge_begin, segment_begin), edge) /
                     denominator;
  return DoublePoint{segment_begin.x + ratio * segment.x,
                     segment_begin.y + ratio * segment.y};
}

std::vector<DoublePoint> convex_intersection(
    const std::vector<DoublePoint>& subject,
    const std::vector<DoublePoint>& clip) {
  auto output = subject;
  const auto orientation = signed_area(clip);
  for (std::size_t edge_index = 0; edge_index < clip.size(); ++edge_index) {
    const auto edge_begin = clip[edge_index];
    const auto edge_end = clip[(edge_index + 1) % clip.size()];
    auto input = std::move(output);
    output.clear();
    if (input.empty()) break;
    auto previous = input.back();
    auto previous_inside =
        inside_clip_edge(previous, edge_begin, edge_end, orientation);
    for (const auto& current : input) {
      const auto current_inside =
          inside_clip_edge(current, edge_begin, edge_end, orientation);
      if (current_inside != previous_inside) {
        output.push_back(
            line_intersection(previous, current, edge_begin, edge_end));
      }
      if (current_inside) output.push_back(current);
      previous = current;
      previous_inside = current_inside;
    }
  }
  return output;
}

struct Bounds {
  double minimum_x = 0;
  double minimum_y = 0;
  double maximum_x = 0;
  double maximum_y = 0;
};

Bounds bounds(const Quad& quad) {
  Bounds result{quad.points[0].x, quad.points[0].y, quad.points[0].x,
                quad.points[0].y};
  for (const auto& point : quad.points) {
    result.minimum_x = std::min(result.minimum_x, static_cast<double>(point.x));
    result.minimum_y = std::min(result.minimum_y, static_cast<double>(point.y));
    result.maximum_x = std::max(result.maximum_x, static_cast<double>(point.x));
    result.maximum_y = std::max(result.maximum_y, static_cast<double>(point.y));
  }
  return result;
}

bool rectangles_overlap(const TileRect& left, const TileRect& right) {
  const auto left_right = static_cast<std::uint64_t>(left.x) + left.width;
  const auto right_right = static_cast<std::uint64_t>(right.x) + right.width;
  const auto left_bottom = static_cast<std::uint64_t>(left.y) + left.height;
  const auto right_bottom = static_cast<std::uint64_t>(right.y) + right.height;
  return static_cast<std::uint64_t>(left.x) < right_right &&
         static_cast<std::uint64_t>(right.x) < left_right &&
         static_cast<std::uint64_t>(left.y) < right_bottom &&
         static_cast<std::uint64_t>(right.y) < left_bottom;
}

bool bounds_overlap(const Bounds& left, const Bounds& right) {
  return left.minimum_x < right.maximum_x && right.minimum_x < left.maximum_x &&
         left.minimum_y < right.maximum_y && right.minimum_y < left.maximum_y;
}

bool duplicates(const TiledCandidate& left, const TiledCandidate& right,
                const TiledDetectionConfig& config) {
  if (left.tile_ordinal == right.tile_ordinal ||
      !rectangles_overlap(left.source_tile, right.source_tile) ||
      !bounds_overlap(bounds(left.global_quad), bounds(right.global_quad))) {
    return false;
  }
  const auto left_polygon = to_polygon(left.global_quad);
  const auto right_polygon = to_polygon(right.global_quad);
  const auto left_area = area(left_polygon);
  const auto right_area = area(right_polygon);
  const auto intersection_area =
      area(convex_intersection(left_polygon, right_polygon));
  if (!std::isfinite(intersection_area) || intersection_area <= 0) return false;
  const auto union_area = left_area + right_area - intersection_area;
  if (union_area <= 0) return false;
  const auto iou = intersection_area / union_area;
  const auto ios = intersection_area / std::min(left_area, right_area);
  return iou >= config.merge_iou_threshold ||
         ios >= config.merge_ios_threshold;
}

}  // namespace

Result<std::vector<std::uint32_t>> plan_detection_axis(
    std::uint32_t length, std::uint32_t tile_side,
    std::uint32_t minimum_overlap) {
  if (length == 0 || tile_side == 0 || minimum_overlap == 0 ||
      minimum_overlap >= tile_side) {
    return failure<std::vector<std::uint32_t>>(
        ErrorCode::invalid_argument, "Tiled detection axis contract is invalid");
  }
  std::vector<std::uint32_t> starts{0};
  if (length <= tile_side) {
    return Result<std::vector<std::uint32_t>>::success(std::move(starts));
  }
  const auto stride = tile_side - minimum_overlap;
  while (static_cast<std::uint64_t>(starts.back()) + tile_side < length) {
    const auto advanced =
        static_cast<std::uint64_t>(starts.back()) + stride;
    const auto anchored = static_cast<std::uint64_t>(length) - tile_side;
    const auto next = std::min(advanced, anchored);
    if (next <= starts.back() ||
        next > std::numeric_limits<std::uint32_t>::max()) {
      return failure<std::vector<std::uint32_t>>(
          ErrorCode::resource_limit_exceeded,
          "Tiled detection axis planning overflowed");
    }
    starts.push_back(static_cast<std::uint32_t>(next));
  }
  return Result<std::vector<std::uint32_t>>::success(std::move(starts));
}

Result<std::vector<TileRect>> plan_detection_tiles(
    std::uint32_t image_width, std::uint32_t image_height,
    const TiledDetectionConfig& config, std::uint32_t max_tiles) {
  if (config.tile_side == 0 || config.minimum_overlap == 0 ||
      config.minimum_overlap >= config.tile_side || max_tiles == 0) {
    return failure<std::vector<TileRect>>(
        ErrorCode::invalid_argument, "Tiled detection contract is invalid");
  }
  auto x_result = plan_detection_axis(image_width, config.tile_side,
                                      config.minimum_overlap);
  if (!x_result) return Result<std::vector<TileRect>>::failure(x_result.error());
  auto y_result = plan_detection_axis(image_height, config.tile_side,
                                      config.minimum_overlap);
  if (!y_result) return Result<std::vector<TileRect>>::failure(y_result.error());
  auto x_starts = std::move(x_result).value();
  auto y_starts = std::move(y_result).value();
  std::uint64_t tile_count = 0;
  if (!checked_mul<std::uint64_t>(x_starts.size(), y_starts.size(),
                                  &tile_count) ||
      tile_count > max_tiles ||
      tile_count > std::numeric_limits<std::uint32_t>::max()) {
    return failure<std::vector<TileRect>>(
        ErrorCode::resource_limit_exceeded,
        "Tiled detection requires too many tiles");
  }
  std::vector<TileRect> result;
  result.reserve(static_cast<std::size_t>(tile_count));
  for (const auto y : y_starts) {
    for (const auto x : x_starts) {
      result.push_back(TileRect{
          static_cast<std::uint32_t>(result.size()), x, y,
          std::min(config.tile_side, image_width - x),
          std::min(config.tile_side, image_height - y)});
    }
  }
  return Result<std::vector<TileRect>>::success(std::move(result));
}

Result<TiledCandidate> make_tiled_candidate(
    const Quad& tile_quad, double db_score, const TileRect& tile,
    std::uint32_t image_width, std::uint32_t image_height,
    std::uint32_t candidate_ordinal,
    std::uint32_t artificial_boundary_margin) {
  if (!valid_convex_quad(tile_quad) || !std::isfinite(db_score) || db_score < 0 ||
      db_score > 1 || tile.width == 0 || tile.height == 0 ||
      static_cast<std::uint64_t>(tile.x) + tile.width > image_width ||
      static_cast<std::uint64_t>(tile.y) + tile.height > image_height) {
    return failure<TiledCandidate>(ErrorCode::postprocess_failed,
                                   "Tiled detection candidate is invalid");
  }
  TiledCandidate result;
  result.db_score = db_score;
  result.tile_ordinal = tile.ordinal;
  result.candidate_ordinal = candidate_ordinal;
  result.source_tile = tile;
  for (std::size_t index = 0; index < tile_quad.points.size(); ++index) {
    const auto x = static_cast<double>(tile_quad.points[index].x) + tile.x;
    const auto y = static_cast<double>(tile_quad.points[index].y) + tile.y;
    result.global_quad.points[index] = Point{
        static_cast<float>(std::clamp(x, 0.0, static_cast<double>(image_width))),
        static_cast<float>(std::clamp(y, 0.0, static_cast<double>(image_height)))};
  }
  if (!valid_convex_quad(result.global_quad)) {
    return failure<TiledCandidate>(ErrorCode::postprocess_failed,
                                   "Restored tiled candidate is invalid");
  }

  const auto candidate_bounds = bounds(result.global_quad);
  const auto tile_right = static_cast<double>(tile.x) + tile.width;
  const auto tile_bottom = static_cast<double>(tile.y) + tile.height;
  const auto fallback_distance =
      static_cast<double>(std::max(image_width, image_height)) + 1.0;
  auto nearest_distance = fallback_distance;
  const auto consider = [&](bool artificial, double distance,
                            ArtificialEdge edge) {
    if (!artificial) return;
    const auto clamped_distance = std::max(0.0, distance);
    nearest_distance = std::min(nearest_distance, clamped_distance);
    if (clamped_distance <= artificial_boundary_margin) {
      result.nearby_artificial_edges = static_cast<std::uint8_t>(
          result.nearby_artificial_edges | static_cast<std::uint8_t>(edge));
    }
  };
  consider(tile.x > 0, candidate_bounds.minimum_x - tile.x, artificial_left);
  consider(tile.y > 0, candidate_bounds.minimum_y - tile.y, artificial_top);
  consider(static_cast<std::uint64_t>(tile.x) + tile.width < image_width,
           tile_right - candidate_bounds.maximum_x, artificial_right);
  consider(static_cast<std::uint64_t>(tile.y) + tile.height < image_height,
           tile_bottom - candidate_bounds.maximum_y, artificial_bottom);
  result.distance_to_nearest_artificial_edge = nearest_distance;
  return Result<TiledCandidate>::success(std::move(result));
}

Result<TiledMergeResult> merge_tiled_candidates(
    std::vector<TiledCandidate> candidates,
    const TiledDetectionConfig& config) {
  if (!std::isfinite(config.merge_iou_threshold) ||
      !std::isfinite(config.merge_ios_threshold) ||
      config.merge_iou_threshold < 0 || config.merge_iou_threshold > 1 ||
      config.merge_ios_threshold < 0 || config.merge_ios_threshold > 1) {
    return failure<TiledMergeResult>(ErrorCode::invalid_argument,
                                     "Tiled merge contract is invalid");
  }
  for (const auto& candidate : candidates) {
    if (!valid_convex_quad(candidate.global_quad) ||
        !std::isfinite(candidate.db_score) || candidate.db_score < 0 ||
        candidate.db_score > 1 ||
        !std::isfinite(candidate.distance_to_nearest_artificial_edge)) {
      return failure<TiledMergeResult>(ErrorCode::postprocess_failed,
                                       "Tiled merge candidate is invalid");
    }
  }
  std::stable_sort(candidates.begin(), candidates.end(),
                   [](const TiledCandidate& left,
                      const TiledCandidate& right) {
                     if (left.is_boundary_candidate() !=
                         right.is_boundary_candidate()) {
                       return !left.is_boundary_candidate();
                     }
                     if (left.db_score != right.db_score) {
                       return left.db_score > right.db_score;
                     }
                     if (left.distance_to_nearest_artificial_edge !=
                         right.distance_to_nearest_artificial_edge) {
                       return left.distance_to_nearest_artificial_edge >
                              right.distance_to_nearest_artificial_edge;
                     }
                     if (left.tile_ordinal != right.tile_ordinal) {
                       return left.tile_ordinal < right.tile_ordinal;
                     }
                     return left.candidate_ordinal < right.candidate_ordinal;
                   });

  TiledMergeResult result;
  result.representatives.reserve(candidates.size());
  result.suppressions.reserve(candidates.size());
  for (auto& candidate : candidates) {
    const auto duplicate = std::find_if(
        result.representatives.begin(), result.representatives.end(),
        [&](const auto& representative) {
          return duplicates(representative, candidate, config);
        });
    if (duplicate != result.representatives.end()) {
      if (result.suppressed_duplicates ==
          std::numeric_limits<std::uint32_t>::max()) {
        return failure<TiledMergeResult>(ErrorCode::resource_limit_exceeded,
                                         "Tiled duplicate count overflowed");
      }
      result.suppressions.push_back(TiledMergeResult::Suppression{
          candidate.tile_ordinal, candidate.candidate_ordinal,
          duplicate->tile_ordinal, duplicate->candidate_ordinal});
      ++result.suppressed_duplicates;
    } else {
      result.representatives.push_back(std::move(candidate));
    }
  }
  return Result<TiledMergeResult>::success(std::move(result));
}

}  // namespace light_ocr::internal
