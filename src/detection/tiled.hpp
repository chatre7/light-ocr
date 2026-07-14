#pragma once

#include <cstdint>
#include <vector>

#include "light_ocr/error.hpp"
#include "light_ocr/types.hpp"
#include "model/bundle_data.hpp"

namespace light_ocr::internal {

struct TileRect {
  std::uint32_t ordinal = 0;
  std::uint32_t x = 0;
  std::uint32_t y = 0;
  std::uint32_t width = 0;
  std::uint32_t height = 0;
};

enum ArtificialEdge : std::uint8_t {
  artificial_left = 1u << 0u,
  artificial_top = 1u << 1u,
  artificial_right = 1u << 2u,
  artificial_bottom = 1u << 3u,
};

struct TiledCandidate {
  Quad global_quad;
  double db_score = 0;
  std::uint32_t tile_ordinal = 0;
  std::uint32_t candidate_ordinal = 0;
  TileRect source_tile;
  std::uint8_t nearby_artificial_edges = 0;
  double distance_to_nearest_artificial_edge = 0;

  bool is_boundary_candidate() const noexcept {
    return nearby_artificial_edges != 0;
  }
};

struct TiledMergeResult {
  struct Suppression {
    std::uint32_t candidate_tile_ordinal = 0;
    std::uint32_t candidate_ordinal = 0;
    std::uint32_t representative_tile_ordinal = 0;
    std::uint32_t representative_ordinal = 0;
  };

  std::vector<TiledCandidate> representatives;
  std::vector<Suppression> suppressions;
  std::uint32_t suppressed_duplicates = 0;
};

Result<std::vector<std::uint32_t>> plan_detection_axis(
    std::uint32_t length, std::uint32_t tile_side,
    std::uint32_t minimum_overlap);

Result<std::vector<TileRect>> plan_detection_tiles(
    std::uint32_t image_width, std::uint32_t image_height,
    const TiledDetectionConfig& config, std::uint32_t max_tiles);

Result<TiledCandidate> make_tiled_candidate(
    const Quad& tile_quad, double db_score, const TileRect& tile,
    std::uint32_t image_width, std::uint32_t image_height,
    std::uint32_t candidate_ordinal,
    std::uint32_t artificial_boundary_margin);

Result<TiledMergeResult> merge_tiled_candidates(
    std::vector<TiledCandidate> candidates,
    const TiledDetectionConfig& config);

}  // namespace light_ocr::internal
