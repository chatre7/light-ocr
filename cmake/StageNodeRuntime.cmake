if(NOT STAGE_DIR OR NOT ADDON_FILE OR NOT RUNTIME_FILES OR
   NOT PLATFORM_ID OR NOT PLATFORM_OS OR NOT PLATFORM_ARCH OR
   NOT RUNTIME_FLAVOR)
  message(FATAL_ERROR "Node runtime staging arguments are incomplete")
endif()

set(_native "${STAGE_DIR}/native")
# A POST_BUILD command may run repeatedly in the same build tree. Recreate the
# package-private payload so aliases or manifests from an earlier flavor cannot
# survive into the new descriptor.
file(REMOVE_RECURSE "${_native}")
file(MAKE_DIRECTORY "${_native}")

function(_light_ocr_copy_record source output_name out_var)
  if(NOT EXISTS "${source}")
    message(FATAL_ERROR "Node runtime artifact is missing: ${source}")
  endif()
  set(_destination "${_native}/${output_name}")
  file(COPY_FILE "${source}" "${_destination}" ONLY_IF_DIFFERENT)
  file(SIZE "${_destination}" _size)
  file(SHA256 "${_destination}" _sha256)
  set(${out_var}
    "{\"path\":\"native/${output_name}\",\"bytes\":${_size},\"sha256\":\"${_sha256}\"}"
    PARENT_SCOPE)
endfunction()

_light_ocr_copy_record("${ADDON_FILE}" "light_ocr_node.node" _addon_record)

# Package only the file named by the addon's loader contract. Link-time and
# fully-versioned aliases are build SDK inputs, not independent runtime payloads.
if(PLATFORM_OS STREQUAL "win32")
  set(_runtime_name "onnxruntime.dll")
elseif(PLATFORM_OS STREQUAL "darwin")
  set(_runtime_name "libonnxruntime.1.22.0.dylib")
elseif(RUNTIME_FLAVOR STREQUAL "webgpu")
  set(_runtime_name "libonnxruntime.so.1")
else()
  set(_runtime_name "libonnxruntime.so.1")
endif()
set(_runtime_source "")
foreach(_runtime_file IN LISTS RUNTIME_FILES)
  cmake_path(GET _runtime_file FILENAME _candidate_name)
  if(_candidate_name STREQUAL _runtime_name)
    if(_runtime_source)
      message(FATAL_ERROR "Multiple Node runtime artifacts match ${_runtime_name}")
    endif()
    set(_runtime_source "${_runtime_file}")
  endif()
endforeach()
if(NOT _runtime_source)
  message(FATAL_ERROR
    "Node runtime loader artifact ${_runtime_name} is absent from: ${RUNTIME_FILES}")
endif()
_light_ocr_copy_record("${_runtime_source}" "${_runtime_name}" _runtime_record)
file(SIZE "${_native}/${_runtime_name}" _runtime_bytes)
file(SHA256 "${_native}/${_runtime_name}" _runtime_sha256)

set(_provider_records
  "\"cpu\":{\"runtimeProvider\":\"CPUExecutionProvider\",\"qualificationId\":\"cpu-baseline-v1\",\"artifacts\":[${_runtime_record}]}")
set(_available_policy "[\"cpu\"]")
set(_runtime_kind "onnxruntime-cpu")
set(_runtime_version "1.22.0")
set(_runtime_abi "onnxruntime-c-api-22")
set(_qualification_only false)
set(_released true)
if(RUNTIME_FLAVOR STREQUAL "webgpu")
  if(NOT WEBGPU_QUALIFICATION_ID)
    message(FATAL_ERROR "WebGPU staging requires a qualification identity")
  endif()
  set(_runtime_kind "onnxruntime-monolithic-webgpu")
  set(_runtime_version "1.23.0")
  set(_runtime_abi "onnxruntime-c-api-23")
  if(QUALIFICATION_ONLY)
    set(_qualification_only true)
    set(_released false)
  else()
    set(_available_policy "[\"webgpu\",\"cpu\"]")
  endif()
  set(_compatibility_name "webgpu-compatibility.json")
  set(_compatibility
    "{\"schemaVersion\":\"1.0\",\"provider\":\"webgpu\",\"platformId\":\"${PLATFORM_ID}\",\"runtimeVersion\":\"${_runtime_version}\",\"runtimeAbi\":\"${_runtime_abi}\",\"qualificationId\":\"${WEBGPU_QUALIFICATION_ID}\",\"qualificationOnly\":${_qualification_only},\"released\":${_released},\"runtimeArtifact\":{\"bytes\":${_runtime_bytes},\"sha256\":\"${_runtime_sha256}\"}}\n")
  file(WRITE "${_native}/${_compatibility_name}" "${_compatibility}")
  file(SIZE "${_native}/${_compatibility_name}" _compatibility_size)
  file(SHA256 "${_native}/${_compatibility_name}" _compatibility_sha256)
  set(_compatibility_record
    "{\"path\":\"native/${_compatibility_name}\",\"bytes\":${_compatibility_size},\"sha256\":\"${_compatibility_sha256}\"}")
  set(_provider_records
    "\"webgpu\":{\"runtimeProvider\":\"WebGpuExecutionProvider\",\"qualificationId\":\"${WEBGPU_QUALIFICATION_ID}\",\"compatibilityManifest\":${_compatibility_record},\"artifacts\":[${_runtime_record},${_compatibility_record}]},${_provider_records}")
endif()
if(HAS_COREML)
  set(_provider_records
    "${_provider_records},\"apple\":{\"runtimeProvider\":\"CoreML\",\"qualificationId\":\"apple-open-macos-v1\",\"artifacts\":[${_addon_record}]}"
  )
  set(_available_policy "[\"apple\",\"cpu\"]")
endif()

set(_libc_field "")
if(PLATFORM_LIBC)
  set(_libc_field ",\"libc\":\"${PLATFORM_LIBC}\"")
endif()
set(_descriptor
  "{\"schemaVersion\":\"1.0\",\"platform\":{\"id\":\"${PLATFORM_ID}\",\"os\":\"${PLATFORM_OS}\",\"architecture\":\"${PLATFORM_ARCH}\"${_libc_field}},\"runtime\":{\"flavor\":\"${RUNTIME_FLAVOR}\",\"kind\":\"${_runtime_kind}\",\"version\":\"${_runtime_version}\",\"abi\":\"${_runtime_abi}\"},\"qualificationOnly\":${_qualification_only},\"released\":${_released},\"autoPolicy\":{\"id\":\"${PLATFORM_ID}-v1\",\"version\":1,\"providers\":${_available_policy}},\"providers\":{${_provider_records}},\"addon\":${_addon_record}}\n"
)
file(WRITE "${_native}/runtime-descriptor.json" "${_descriptor}")
