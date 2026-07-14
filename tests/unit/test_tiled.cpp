#include <algorithm>
#include <cstdint>
#include <limits>
#include <utility>
#include <vector>

#include "detection/tiled.hpp"
#include "light_ocr/error.hpp"
#include "light_ocr/types.hpp"
#include "model/bundle_data.hpp"
#include "test.hpp"

using namespace light_ocr;

namespace {

internal::TiledDetectionConfig tiled_config() {
  return internal::TiledDetectionConfig{"tiled-v1", 1280, 128, 32, 32,
                                        0.5, 0.8};
}

Quad rectangle(float left, float top, float right, float bottom) {
  return Quad{{Point{left, top}, Point{right, top}, Point{right, bottom},
               Point{left, bottom}}};
}

internal::TiledCandidate candidate(const Quad& local, double score,
                                   const internal::TileRect& tile,
                                   std::uint32_t ordinal) {
  auto result = internal::make_tiled_candidate(local, score, tile, 2048, 2048,
                                                ordinal, 32);
  if (!result) {
    light_ocr::test::fail("make_tiled_candidate", __FILE__, __LINE__);
  }
  return std::move(result).value();
}

}  // namespace

LIGHT_OCR_TEST(tiled_axis_planner_anchors_the_final_full_tile) {
  const auto one = internal::plan_detection_axis(1280, 1280, 128);
  EXPECT_TRUE(one);
  EXPECT_EQ(one.value(), std::vector<std::uint32_t>({0}));

  const auto one_pixel = internal::plan_detection_axis(1281, 1280, 128);
  EXPECT_TRUE(one_pixel);
  EXPECT_EQ(one_pixel.value(), std::vector<std::uint32_t>({0, 1}));

  const auto image_2048 = internal::plan_detection_axis(2048, 1280, 128);
  EXPECT_TRUE(image_2048);
  EXPECT_EQ(image_2048.value(), std::vector<std::uint32_t>({0, 768}));

  const auto maximum = internal::plan_detection_axis(10000, 1280, 128);
  EXPECT_TRUE(maximum);
  EXPECT_EQ(maximum.value().size(), 9u);
  EXPECT_EQ(maximum.value().front(), 0u);
  EXPECT_EQ(maximum.value().back(), 8720u);

  for (const auto length : {1u, 31u, 32u, 1279u, 1280u, 1281u, 2048u,
                            4096u, 10000u}) {
    const auto planned = internal::plan_detection_axis(length, 1280, 128);
    EXPECT_TRUE(planned);
    EXPECT_EQ(planned.value().front(), 0u);
    EXPECT_TRUE(planned.value().back() <= length);
    std::uint64_t covered_until = 0;
    for (std::size_t index = 0; index < planned.value().size(); ++index) {
      const auto start = planned.value()[index];
      if (index != 0) {
        EXPECT_TRUE(start > planned.value()[index - 1]);
        EXPECT_TRUE(start <= covered_until);
      }
      covered_until = std::max<std::uint64_t>(
          covered_until, static_cast<std::uint64_t>(start) + 1280);
    }
    EXPECT_TRUE(covered_until >= length);
    if (length > 1280) {
      EXPECT_EQ(planned.value().back(), length - 1280);
    }
  }

  EXPECT_FALSE(internal::plan_detection_axis(0, 1280, 128));
  EXPECT_FALSE(internal::plan_detection_axis(2048, 0, 128));
  EXPECT_FALSE(internal::plan_detection_axis(2048, 1280, 0));
  EXPECT_FALSE(internal::plan_detection_axis(2048, 1280, 1280));
}

LIGHT_OCR_TEST(tiled_merge_locks_iou_threshold_on_both_sides) {
  const internal::TileRect left{0, 0, 0, 1280, 1280};
  const internal::TileRect right{1, 768, 0, 1280, 1280};
  const auto first = candidate(rectangle(900, 100, 1200, 200), 0.9, left, 0);
  auto config = tiled_config();
  config.merge_ios_threshold = 1.0;

  for (const auto& [right_left, expected_count] :
       std::vector<std::pair<float, std::size_t>>{{1001, 2}, {1000, 1},
                                                  {999, 1}}) {
    const auto second = candidate(
        rectangle(right_left - 768, 100, right_left - 768 + 300, 200),
        0.8, right, 0);
    const auto merged =
        internal::merge_tiled_candidates({first, second}, config);
    EXPECT_TRUE(merged);
    EXPECT_EQ(merged.value().representatives.size(), expected_count);
  }
}

LIGHT_OCR_TEST(tiled_merge_locks_ios_threshold_on_both_sides) {
  const internal::TileRect left{0, 0, 0, 1280, 1280};
  const internal::TileRect right{1, 768, 0, 1280, 1280};
  const auto first = candidate(rectangle(900, 100, 1200, 200), 0.9, left, 0);
  auto config = tiled_config();
  config.merge_iou_threshold = 1.0;

  for (const auto& [right_left, expected_count] :
       std::vector<std::pair<float, std::size_t>>{{1121, 2}, {1120, 1},
                                                  {1119, 1}}) {
    const auto second = candidate(
        rectangle(right_left - 768, 100, right_left - 768 + 100, 200),
        0.8, right, 0);
    const auto merged =
        internal::merge_tiled_candidates({first, second}, config);
    EXPECT_TRUE(merged);
    EXPECT_EQ(merged.value().representatives.size(), expected_count);
  }
}

LIGHT_OCR_TEST(tiled_planner_is_row_major_and_checks_the_global_limit) {
  const auto plan =
      internal::plan_detection_tiles(2048, 2048, tiled_config(), 100);
  EXPECT_TRUE(plan);
  EXPECT_EQ(plan.value().size(), 4u);
  EXPECT_EQ(plan.value()[0].x, 0u);
  EXPECT_EQ(plan.value()[0].y, 0u);
  EXPECT_EQ(plan.value()[1].x, 768u);
  EXPECT_EQ(plan.value()[1].y, 0u);
  EXPECT_EQ(plan.value()[2].x, 0u);
  EXPECT_EQ(plan.value()[2].y, 768u);
  EXPECT_EQ(plan.value()[3].ordinal, 3u);

  const auto limited =
      internal::plan_detection_tiles(10000, 10000, tiled_config(), 80);
  EXPECT_FALSE(limited);
  EXPECT_EQ(limited.error().code, ErrorCode::resource_limit_exceeded);
}

LIGHT_OCR_TEST(tiled_candidate_distinguishes_artificial_and_original_edges) {
  const internal::TileRect top_left{0, 0, 0, 1280, 1280};
  const auto original_edge = candidate(rectangle(0, 100, 80, 140), 0.8,
                                       top_left, 0);
  EXPECT_FALSE(original_edge.is_boundary_candidate());

  const auto artificial_edge = candidate(
      rectangle(1200, 100, 1280, 140), 0.8, top_left, 1);
  EXPECT_TRUE(artificial_edge.is_boundary_candidate());
  EXPECT_TRUE((artificial_edge.nearby_artificial_edges &
               internal::artificial_right) != 0);
}

LIGHT_OCR_TEST(tiled_merge_prefers_complete_non_boundary_candidate) {
  const internal::TileRect left{0, 0, 0, 1280, 1280};
  const internal::TileRect right{1, 768, 0, 1280, 1280};
  auto clipped = candidate(rectangle(1200, 100, 1280, 150), 0.99, left, 0);
  auto complete =
      candidate(rectangle(432, 100, 532, 150), 0.75, right, 0);
  EXPECT_TRUE(clipped.is_boundary_candidate());
  EXPECT_FALSE(complete.is_boundary_candidate());

  auto merged = internal::merge_tiled_candidates(
      {std::move(clipped), std::move(complete)}, tiled_config());
  EXPECT_TRUE(merged);
  EXPECT_EQ(merged.value().representatives.size(), 1u);
  EXPECT_EQ(merged.value().suppressed_duplicates, 1u);
  EXPECT_EQ(merged.value().suppressions.size(), 1u);
  EXPECT_EQ(merged.value().suppressions[0].candidate_tile_ordinal, 0u);
  EXPECT_EQ(merged.value().suppressions[0].representative_tile_ordinal, 1u);
  EXPECT_EQ(merged.value().representatives[0].tile_ordinal, 1u);
}

LIGHT_OCR_TEST(tiled_merge_never_suppresses_candidates_from_the_same_tile) {
  const internal::TileRect tile{0, 0, 0, 1280, 1280};
  auto first = candidate(rectangle(100, 100, 300, 150), 0.9, tile, 0);
  auto second = candidate(rectangle(100, 100, 300, 150), 0.8, tile, 1);
  auto merged = internal::merge_tiled_candidates(
      {std::move(first), std::move(second)}, tiled_config());
  EXPECT_TRUE(merged);
  EXPECT_EQ(merged.value().representatives.size(), 2u);
  EXPECT_EQ(merged.value().suppressed_duplicates, 0u);
}

LIGHT_OCR_TEST(tiled_merge_is_independent_of_input_candidate_order) {
  const internal::TileRect left{0, 0, 0, 1280, 1280};
  const internal::TileRect right{1, 768, 0, 1280, 1280};
  auto left_candidate =
      candidate(rectangle(900, 100, 1100, 150), 0.8, left, 0);
  auto right_candidate =
      candidate(rectangle(132, 100, 332, 150), 0.9, right, 0);
  auto forward = internal::merge_tiled_candidates(
      {left_candidate, right_candidate}, tiled_config());
  auto reverse = internal::merge_tiled_candidates(
      {right_candidate, left_candidate}, tiled_config());
  EXPECT_TRUE(forward);
  EXPECT_TRUE(reverse);
  EXPECT_EQ(forward.value().representatives.size(), 1u);
  EXPECT_EQ(reverse.value().representatives.size(), 1u);
  EXPECT_EQ(forward.value().representatives[0].tile_ordinal,
            reverse.value().representatives[0].tile_ordinal);
}

LIGHT_OCR_TEST(tiled_merge_is_greedy_and_non_transitive) {
  const internal::TileRect first_tile{0, 0, 0, 1280, 1280};
  const internal::TileRect second_tile{1, 400, 0, 1280, 1280};
  const internal::TileRect third_tile{2, 768, 0, 1280, 1280};
  const auto first =
      candidate(rectangle(850, 300, 1150, 400), 0.9, first_tile, 0);
  const auto middle =
      candidate(rectangle(550, 300, 850, 400), 0.8, second_tile, 0);
  const auto last =
      candidate(rectangle(282, 300, 582, 400), 0.7, third_tile, 0);
  const auto merged =
      internal::merge_tiled_candidates({middle, last, first}, tiled_config());
  EXPECT_TRUE(merged);
  EXPECT_EQ(merged.value().representatives.size(), 2u);
  EXPECT_EQ(merged.value().suppressed_duplicates, 1u);
  EXPECT_EQ(merged.value().representatives[0].tile_ordinal, 0u);
  EXPECT_EQ(merged.value().representatives[1].tile_ordinal, 2u);
}

LIGHT_OCR_TEST(tiled_merge_rejects_invalid_geometry_and_contracts) {
  const internal::TileRect tile{0, 0, 0, 1280, 1280};
  auto invalid_score = internal::make_tiled_candidate(
      rectangle(100, 100, 200, 200),
      std::numeric_limits<double>::quiet_NaN(), tile, 2048, 2048, 0, 32);
  EXPECT_FALSE(invalid_score);

  Quad self_crossing{{Point{100, 100}, Point{200, 200}, Point{200, 100},
                      Point{100, 200}}};
  EXPECT_FALSE(internal::make_tiled_candidate(
      self_crossing, 0.9, tile, 2048, 2048, 0, 32));

  Quad non_finite = rectangle(100, 100, 200, 200);
  non_finite.points[1].x = std::numeric_limits<float>::infinity();
  EXPECT_FALSE(internal::make_tiled_candidate(
      non_finite, 0.9, tile, 2048, 2048, 0, 32));

  auto config = tiled_config();
  config.merge_iou_threshold = std::numeric_limits<double>::quiet_NaN();
  EXPECT_FALSE(internal::merge_tiled_candidates({}, config));
}

LIGHT_OCR_TEST(tiled_merge_handles_rotated_quads) {
  const internal::TileRect left{0, 0, 0, 1280, 1280};
  const internal::TileRect right{1, 768, 0, 1280, 1280};
  const Quad first_quad{{Point{1000, 100}, Point{1100, 150},
                         Point{1000, 200}, Point{900, 150}}};
  const Quad second_quad{{Point{232, 100}, Point{332, 150},
                          Point{232, 200}, Point{132, 150}}};
  const auto first = candidate(first_quad, 0.9, left, 0);
  const auto second = candidate(second_quad, 0.8, right, 0);
  const auto merged =
      internal::merge_tiled_candidates({second, first}, tiled_config());
  EXPECT_TRUE(merged);
  EXPECT_EQ(merged.value().representatives.size(), 1u);
  EXPECT_EQ(merged.value().suppressed_duplicates, 1u);
}

LIGHT_OCR_TEST(tiled_merge_locks_distance_tile_and_candidate_tie_breaks) {
  const internal::TileRect left{0, 0, 0, 1280, 1280};
  const internal::TileRect right{1, 768, 0, 1280, 1280};
  auto nearer = candidate(rectangle(900, 500, 1100, 550), 0.8, left, 0);
  auto farther =
      candidate(rectangle(132, 500, 332, 550), 0.8, right, 0);
  nearer.nearby_artificial_edges = internal::artificial_right;
  farther.nearby_artificial_edges = internal::artificial_left;
  nearer.distance_to_nearest_artificial_edge = 4;
  farther.distance_to_nearest_artificial_edge = 8;
  auto distance_merged =
      internal::merge_tiled_candidates({nearer, farther}, tiled_config());
  EXPECT_TRUE(distance_merged);
  EXPECT_EQ(distance_merged.value().representatives.size(), 1u);
  EXPECT_EQ(distance_merged.value().representatives[0].tile_ordinal, 1u);

  farther.distance_to_nearest_artificial_edge = 4;
  auto tile_merged =
      internal::merge_tiled_candidates({farther, nearer}, tiled_config());
  EXPECT_TRUE(tile_merged);
  EXPECT_EQ(tile_merged.value().representatives[0].tile_ordinal, 0u);

  auto later = candidate(rectangle(100, 700, 300, 750), 0.8, left, 9);
  auto earlier = candidate(rectangle(100, 700, 300, 750), 0.8, left, 2);
  auto candidate_order =
      internal::merge_tiled_candidates({later, earlier}, tiled_config());
  EXPECT_TRUE(candidate_order);
  EXPECT_EQ(candidate_order.value().representatives.size(), 2u);
  EXPECT_EQ(candidate_order.value().representatives[0].candidate_ordinal, 2u);
  EXPECT_EQ(candidate_order.value().representatives[1].candidate_ordinal, 9u);
}

LIGHT_OCR_TEST(tiled_merge_does_not_compare_non_overlapping_source_tiles) {
  internal::TiledCandidate first;
  first.global_quad = rectangle(100, 100, 300, 200);
  first.db_score = 0.9;
  first.tile_ordinal = 0;
  first.source_tile = {0, 0, 0, 500, 500};
  first.distance_to_nearest_artificial_edge = 100;
  auto second = first;
  second.db_score = 0.8;
  second.tile_ordinal = 1;
  second.source_tile = {1, 700, 0, 500, 500};
  const auto merged =
      internal::merge_tiled_candidates({first, second}, tiled_config());
  EXPECT_TRUE(merged);
  EXPECT_EQ(merged.value().representatives.size(), 2u);
  EXPECT_EQ(merged.value().suppressed_duplicates, 0u);
}
