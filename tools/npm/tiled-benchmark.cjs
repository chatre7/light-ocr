'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const GIB = 1024 * 1024 * 1024;
const NODE_ABSOLUTE_LIMIT = GIB + 64 * 1024 * 1024;

function argument(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  if (index === -1) return fallback;
  if (index + 1 >= process.argv.length) throw new Error(`missing value for --${name}`);
  return process.argv[index + 1];
}

function positiveInteger(name, fallback) {
  const value = Number(argument(name, String(fallback)));
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`--${name} must be a positive integer`);
  }
  return value;
}

function distribution(values) {
  const ordered = [...values].sort((left, right) => left - right);
  const nearestRank = (percentile) => ordered[Math.min(
    ordered.length - 1,
    Math.ceil(percentile * ordered.length) - 1,
  )];
  return {
    minimum: ordered[0],
    median: nearestRank(0.5),
    p95: nearestRank(0.95),
    maximum: ordered.at(-1),
  };
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonical(value[key])}`,
    ).join(',')}}`;
  }
  return JSON.stringify(value);
}

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function stableResult(result) {
  return {
    modelBundleId: result.modelBundleId,
    lines: result.lines,
    diagnostics: result.diagnostics,
  };
}

function normalizeText(value) {
  return value.normalize('NFKC');
}

async function main() {
  const fixtureArgument = argument('fixture');
  const reportArgument = argument('report');
  if (!fixtureArgument || !reportArgument) {
    throw new Error('--fixture and --report are required');
  }
  const fixtureDirectory = path.resolve(fixtureArgument);
  const reportPath = path.resolve(reportArgument);
  const warmup = positiveInteger('warmup', 5);
  const iterations = positiveInteger('iterations', 10);
  const platformId = argument('platform-id', `${process.platform}-${process.arch}`);
  const bundlePath = argument('bundle');
  const diagnosticsMode = argument('diagnostics', 'on');
  if (!['on', 'off'].includes(diagnosticsMode)) {
    throw new Error('--diagnostics must be on or off');
  }
  const metadata = JSON.parse(
    fs.readFileSync(path.join(fixtureDirectory, 'fixture.json'), 'utf8'),
  );
  const pixels = fs.readFileSync(path.join(fixtureDirectory, 'pixels.bin'));
  assert.equal(sha256(pixels), metadata.pixelSha256, 'fixture pixel hash mismatch');

  const expected = metadata.annotations
    .slice()
    .sort((left, right) => left.order - right.order)
    .map((annotation) => normalizeText(annotation.text));
  const packagePath = argument('package');
  const packageSpecifier = packagePath
    ? path.resolve(packagePath)
    : '@arcships/light-ocr';
  const packageRoot = packagePath
    ? path.resolve(packagePath)
    : path.dirname(path.dirname(require.resolve(packageSpecifier)));
  const packageManifest = JSON.parse(
    fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'),
  );
  const { createEngine } = require(packageSpecifier);
  const loadBegin = process.hrtime.bigint();
  const engine = await createEngine({
    ...(bundlePath ? { bundlePath: path.resolve(bundlePath) } : {}),
    detection: { strategy: 'tiled' },
    intraOpThreads: 1,
    interOpThreads: 1,
  });
  const loadEnd = process.hrtime.bigint();
  const image = {
    data: pixels,
    width: metadata.width,
    height: metadata.height,
    stride: metadata.stride,
    pixelFormat: metadata.pixelFormat,
  };
  const latencyUs = [];
  const inferenceOnlyUs = [];
  const resultHashes = [];
  const rssSamples = [];
  let lastResult;
  try {
    assert.equal(engine.info.detectionStrategy, 'tiled');
    assert.equal(engine.info.tiledDetection.contractVersion, 'tiled-v1');
    assert.equal(engine.info.intraOpThreads, 1);
    assert.equal(engine.info.interOpThreads, 1);
    for (let index = 0; index < warmup; ++index) {
      await engine.recognize(image);
    }
    for (let index = 0; index < iterations; ++index) {
      const begin = process.hrtime.bigint();
      const result = await engine.recognize(image, {
        includeDiagnostics: diagnosticsMode === 'on',
      });
      const end = process.hrtime.bigint();
      latencyUs.push(Number((end - begin) / 1000n));
      inferenceOnlyUs.push(
        result.timingUs.detectionInference + result.timingUs.recognitionInference,
      );
      resultHashes.push(sha256(canonical(stableResult(result))));
      rssSamples.push(process.memoryUsage.rss());
      lastResult = result;
    }
  } finally {
    await engine.close();
  }

  const diagnostics = lastResult.diagnostics;
  const observed = lastResult.lines.map((line) => normalizeText(line.text));
  const maxRssKilobytes = process.resourceUsage().maxRSS;
  const peakResident = Number.isFinite(maxRssKilobytes) && maxRssKilobytes > 0
    ? maxRssKilobytes * 1024
    : 0;
  const resultStable = new Set(resultHashes).size === 1;
  const gates = {
    exactTextAndOrder: observed.length === expected.length
      && observed.every((value, index) => value === expected[index]),
    resultStable,
    peakMeasurement: peakResident > 0,
    absolutePeak: peakResident > 0 && peakResident <= NODE_ABSOLUTE_LIMIT,
    tiledContract: diagnosticsMode === 'off' || (
      diagnostics.detectionPasses.length === 4
      && diagnostics.maxLiveDetectionPassBuffers === 1
      && diagnostics.detectionPasses.every(
        (item) => item.tensorWidth <= 1280 && item.tensorHeight <= 1280,
      )
    ),
    candidateAccounting: diagnosticsMode === 'off'
      || diagnostics.rawDetectionBoxes
        - diagnostics.suppressedDuplicateBoxes === diagnostics.acceptedBoxes,
    sampleCount: latencyUs.length === iterations && iterations >= 10,
    perCallTimeout: latencyUs.every((value) => value < 120 * 1000 * 1000),
  };
  const passed = Object.values(gates).every(Boolean);
  const report = {
    schema: 'light-ocr-tiled-node-report/1.0',
    passed,
    contractVersion: 'tiled-v1',
    fixtureId: metadata.id,
    fixtureSha256: sha256(fs.readFileSync(path.join(fixtureDirectory, 'fixture.json'))),
    pixelSha256: metadata.pixelSha256,
    platformId,
    runtime: {
      node: process.version,
      napi: process.versions.napi,
      packageVersion: packageManifest.version,
      platform: process.platform,
      arch: process.arch,
      osRelease: os.release(),
      cpu: os.cpus()[0]?.model ?? 'unknown',
      logicalCpus: os.cpus().length,
      totalMemoryBytes: os.totalmem(),
      intraOpThreads: 1,
      interOpThreads: 1,
      engineInfo: engine.info,
    },
    loadUs: Number((loadEnd - loadBegin) / 1000n),
    warmup,
    iterations,
    diagnosticsMode,
    latencyUs: distribution(latencyUs),
    inferenceOnlyUs: distribution(inferenceOnlyUs),
    memoryBytes: {
      peakResident,
      residentMinimum: Math.min(...rssSamples),
      residentMaximum: Math.max(...rssSamples),
      residentFinal: rssSamples.at(-1),
      absoluteMaximum: NODE_ABSOLUTE_LIMIT,
    },
    result: {
      acceptedLines: lastResult.lines.length,
      ...(diagnostics ? {
        rawDetectionBoxes: diagnostics.rawDetectionBoxes,
        suppressedDuplicateBoxes: diagnostics.suppressedDuplicateBoxes,
        acceptedBoxes: diagnostics.acceptedBoxes,
        detectionPasses: diagnostics.detectionPasses,
      } : {}),
      stableSha256: resultHashes[0],
    },
    gates,
  };
  fs.mkdirSync(path.dirname(reportPath), { recursive: true });
  fs.writeFileSync(reportPath, `${JSON.stringify(report)}\n`);
  process.stdout.write(`${JSON.stringify(report)}\n`);
  if (!passed) process.exitCode = 1;
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 2;
});
