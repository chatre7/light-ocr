#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "light_ocr/core.hpp"

namespace light_ocr::internal {

struct RuntimePolicy {
  std::string id;
  std::uint32_t version = 0;
  std::string platform_id;
  std::string runtime_flavor;
  std::string runtime_version;
  std::string runtime_abi;
  bool qualification_only = false;
  bool released = true;
  std::vector<std::string> ordered_candidates;
  std::vector<std::string> available_providers;
  // Entries are aligned with available_providers.
  std::vector<std::string> provider_qualification_ids;
};

RuntimePolicy builtin_runtime_policy();

class EngineFactory {
 public:
  static Result<std::unique_ptr<Engine>> create(
      ModelBundle bundle, const EngineOptions& options,
      RuntimePolicy runtime_policy);
};

}  // namespace light_ocr::internal
