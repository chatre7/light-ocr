# C++ Core 与 Node-API 实施状态

更新时间：2026-07-14  
结论：C++ Core 第一阶段高分辨率优化与 Node-API v1 已完成，`@arcships/light-ocr@0.1.0` 已公开发布。默认 bounded/960、batch 1 流式 recognition、schema 1.1 bundle、双 profile parity、四平台 Core CI、Node.js 22/24 八组 package matrix、registry 安装和禁网运行门槛均已通过。`tiled` 属于后续准确模式，不阻塞 `0.1.0`；其[技术设计与验收规格](tiled-design-and-acceptance.md)已形成 Draft，但实现、语料和四平台基线仍为 Pending。

状态含义：

- **Done**：代码存在，并有本地实际运行证据。
- **Configured**：自动化已写好，但当前工作区未产生真实远端 run 证据。
- **Pending**：需要外部平台或制品仓动作。

## 需求验收矩阵

| `requirements.md` §19 条目 | 状态 | 当前证据或缺口 |
| --- | --- | --- |
| 四个 Tier 1 原生构建/测试 | Done | Release commit 的 [Core run 29312484043](https://github.com/arcships/light-ocr/actions/runs/29312484043) 六个 jobs 全部成功。 |
| 生产 Core 无 Python、无子进程 | Done | `light_ocr_core` 仅 C++；Python 只在 oracle/generator/report tools；Core 无 process/shell API。 |
| raw-pixel 公共 API、ownership/lifecycle 文档 | Done | `include/light_ocr/*.hpp` 与 [native-api.md](native-api.md)。 |
| detection/geometry/crop/recognition/decode 分层与测试 | Done | 独立源码模块、unit tests、stage probe 和真实模型 integration tests。 |
| PP-OCRv6 bundle 固定、哈希、许可、离线可用 | Done | `ppocrv6-small-onnx-20260714.1`、schema 1.1、原始归档、成员、dictionary、manifest 和 USTAR 已锁定；npm model tarball 为 26,091,093 bytes，registry integrity 已记录。 |
| stage 与 final parity | Done | `upstream_exact` 与 `bounded_default` 均为 14/14；候选级 trace 完整；release commit 的 oracle 与四平台 jobs 全绿。 |
| 首 bundle ground-truth quality report | Done（本机） | bounded 默认在 10 个锁定 fixtures 上 10/10 exact、CER `0`；IoU≥0.5 下 detection precision/recall/Hmean 均为 `1.0`。旧 exact 基线仍独立保留。 |
| 相对性能门槛 | Done（参考本机） | bounded 默认：median `0.9824867× ≤ 1.10×`；p95 `1.0139793× ≤ 1.15×`；inference median `0.9961966× ≤ 1.05×`。受控 CI worker 报告仍应保留。 |
| Sanitizer、fuzz、leak、lifecycle、malformed input | Done | 本机 ASan+UBSan、TSan、standalone fuzz、lifecycle 和 malformed model/tensor 已通过；release Core safety job 的 sanitizers、TSan 和 libFuzzer smoke 全绿。 |
| 无 network/shell/cwd/locale 运行依赖 | Done | sterile cwd/minimal env 与 Linux network namespace disabled 测试通过；npm release 另完成已安装 package 的禁网运行。 |
| manifest、hash、licenses、SBOM、parity、benchmark | Done | Release commit 已重新生成并保存四平台 metadata、六个 npm tarballs 的 hashes/integrity、parity、quality 与 benchmark 证据。 |
| N-API/npm 非本 Core milestone | Done / `0.1.0` published | raw Node-API v8、CJS/ESM、`.d.ts`、内置模型解析、四平台 prebuild、双重背压、AbortSignal 与生命周期均已完成；[npm release run 29312486301](https://github.com/arcships/light-ocr/actions/runs/29312486301) 的 Node 22/24 八组测试、registry 分阶段发布和禁网复验全绿。 |
| 高分辨率峰值内存 | Done | Release 原生独立进程本机参考：2048² 空白 `318.8 MiB ≤ 384 MiB`；xfund 密集表单 116 框 `400.5 MiB ≤ 640 MiB`。四平台 release jobs 的真实模型与 RSS gates 均通过。 |
| Tiled 高分辨率准确模式 | Pending / design drafted | 独立规格已固定 `tiled-v1` planner、merge、C++/Node additive API、八张 2048² ground-truth fixtures、四平台 peak/latency 和六包发布门槛；代码、corpus、baseline 尚未实现，不得声明已支持。 |

## 本机最终验证快照

环境：macOS arm64，Apple Clang 21.0.0，CMake 4.2.1，macOS deployment target 13.3，ONNX Runtime CPU，intra/inter-op threads 均为 1。

| 验证 | 结果 |
| --- | --- |
| Release CTest | 16/16 passed；包括 unit、integration、双 profile golden/parity、quality 和两项原生 memory gate |
| 全阶段语料 | `upstream_exact` 14/14；`bounded_default` 14/14 |
| 质量基线 | bounded 10/10 exact；0/104 CER；10 TP / 0 FP / 0 FN，detection P/R/Hmean = 1.0（IoU≥0.5） |
| ASan + UBSan | 2/2 passed；Apple 平台不启用 LSan |
| TSan | 2/2 passed |
| standalone fuzz | image 100k、bundle 100k、geometry 100k、lifecycle 10，全部完成 |
| leak/RSS | 5 warmup + 10 cycles；当前复测 growth 21,413,888 bytes（2,141,388 bytes/cycle）；gate 32 MiB / 8 MiB per cycle |
| offline contract | sterile cwd/minimal locale environment passed |
| model archive | 31,334,400 bytes；SHA-256 `74e246bf075c141da51e58515c731298fdabee9fd5bd8feb7cf6c7f4f352de17` |
| Node-API v1 | Node.js 22.13.0；macOS arm64 Release/Werror 构建；CTest 3/3；bounded/exact 映射、真实 PP-OCRv6 API、snapshot/byteOffset、校验、symlink root、双重背压、abort、heartbeat、close/worker teardown 测试通过 |

性能报告（5 warmup + 30 iterations，`generated-hello-123`）：

| 指标 | Native | Python oracle | 比率 | 门槛 |
| --- | ---: | ---: | ---: | ---: |
| warm median end-to-end | 75,678 µs | 77,027 µs | 0.9824867× | ≤ 1.10× |
| warm p95 end-to-end | 79,788 µs | 78,688 µs | 1.0139793× | ≤ 1.15× |
| inference-only median | 74,125 µs | 74,408 µs | 0.9961966× | ≤ 1.05× |

这些数值是这台机器上的验收快照，不是所有硬件的绝对性能承诺。

## 首发结论与后续范围

`0.1.0` 的四平台 Core、Node.js 22/24 prebuild、六包确定性制品、public registry、provenance、默认 `createEngine()` 与禁网运行证据已经完成，详见 [npm 0.1.0 发布记录](releases/npm-0.1.0.md)。

后续工作包括 C++ 安装包与稳定 ABI、`tiled` 准确模式、更多模型、Electron/Bun、签名/公证和非 npm 分发；这些都不是 `0.1.0` 已声明能力，也不影响本次 npm 发布结论。
