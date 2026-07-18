'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

function adapterError(code, message, detail, cause) {
  const error = new Error(message, cause === undefined ? undefined : { cause });
  error.name = 'OcrError';
  error.code = code;
  if (detail) error.detail = detail;
  return error;
}

function platformIdentity() {
  const key = `${process.platform}-${process.arch}`;
  const identities = {
    'darwin-arm64': { id: 'macos-arm64', os: 'darwin', architecture: 'arm64' },
    'darwin-x64': { id: 'macos-x64', os: 'darwin', architecture: 'x86_64' },
    'win32-x64': { id: 'windows-x64', os: 'win32', architecture: 'x86_64' },
  };
  if (key === 'linux-x64') {
    const report = process.report?.getReport?.();
    if (report?.header?.glibcVersionRuntime) {
      return { id: 'linux-x64', os: 'linux', architecture: 'x86_64', libc: 'glibc' };
    }
    throw adapterError(
      'unsupported_platform',
      'light-ocr currently supports Linux x64 with glibc only',
      key,
    );
  }
  const identity = identities[key];
  if (!identity) {
    throw adapterError('unsupported_platform', `light-ocr does not support ${key}`, key);
  }
  return identity;
}

function platformPackage() {
  const packages = {
    'macos-arm64': '@arcships/light-ocr-darwin-arm64',
    'macos-x64': '@arcships/light-ocr-darwin-x64',
    'windows-x64': '@arcships/light-ocr-win32-x64',
    'linux-x64': '@arcships/light-ocr-linux-x64-gnu',
  };
  return packages[platformIdentity().id];
}

function safeArtifactPath(root, value, field) {
  if (typeof value !== 'string' || value === '' || value.includes('\0')) {
    throw adapterError('package_load_failed', `${field} must be a package-relative path`);
  }
  const normalized = value.replaceAll('\\', '/');
  if (
    path.posix.isAbsolute(normalized) ||
    /^[A-Za-z]:/.test(normalized) ||
    normalized.split('/').some((part) => part === '..' || part === '')
  ) {
    throw adapterError('package_load_failed', `${field} escapes the native package`, value);
  }
  const resolved = path.resolve(root, ...normalized.split('/'));
  const relative = path.relative(path.resolve(root), resolved);
  if (relative.startsWith('..') || path.isAbsolute(relative)) {
    throw adapterError('package_load_failed', `${field} escapes the native package`, value);
  }
  return resolved;
}

function sha256(filename) {
  return crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
}

function verifyArtifact(root, artifact, field) {
  if (!artifact || typeof artifact !== 'object' || Array.isArray(artifact)) {
    throw adapterError('package_load_failed', `${field} must be an object`);
  }
  const filename = safeArtifactPath(root, artifact.path, `${field}.path`);
  let stats;
  try {
    stats = fs.lstatSync(filename);
  } catch (cause) {
    throw adapterError('package_load_failed', `Descriptor artifact is missing`, artifact.path, cause);
  }
  if (!stats.isFile() || stats.isSymbolicLink()) {
    throw adapterError('package_load_failed', 'Descriptor artifact is not a regular file', artifact.path);
  }
  if (!Number.isSafeInteger(artifact.bytes) || artifact.bytes < 0 || stats.size !== artifact.bytes) {
    throw adapterError('package_load_failed', 'Descriptor artifact byte count mismatch', artifact.path);
  }
  if (!/^[a-f0-9]{64}$/.test(artifact.sha256 || '') || sha256(filename) !== artifact.sha256) {
    throw adapterError('package_load_failed', 'Descriptor artifact hash mismatch', artifact.path);
  }
  return filename;
}

function validateRuntimeDescriptor(descriptorPath) {
  const absoluteDescriptor = path.resolve(descriptorPath);
  let descriptor;
  try {
    const stats = fs.lstatSync(absoluteDescriptor);
    if (!stats.isFile() || stats.isSymbolicLink()) throw new Error('not a regular file');
    descriptor = JSON.parse(fs.readFileSync(absoluteDescriptor, 'utf8'));
  } catch (cause) {
    throw adapterError(
      'package_load_failed',
      'Unable to read the native runtime descriptor',
      absoluteDescriptor,
      cause,
    );
  }
  const root = path.dirname(path.dirname(absoluteDescriptor));
  if (!descriptor || descriptor.schemaVersion !== '1.0') {
    throw adapterError('package_load_failed', 'Unsupported native runtime descriptor schema');
  }
  const expected = platformIdentity();
  const actual = descriptor.platform;
  if (
    !actual ||
    actual.id !== expected.id ||
    actual.os !== expected.os ||
    actual.architecture !== expected.architecture ||
    (expected.libc && actual.libc !== expected.libc)
  ) {
    throw adapterError('package_load_failed', 'Native runtime descriptor platform mismatch');
  }
  if (typeof descriptor.released !== 'boolean' || typeof descriptor.qualificationOnly !== 'boolean') {
    throw adapterError('package_load_failed', 'Native runtime descriptor release flags are invalid');
  }
  const policy = descriptor.autoPolicy;
  if (
    !policy ||
    typeof policy.id !== 'string' ||
    policy.id === '' ||
    !Number.isSafeInteger(policy.version) ||
    policy.version < 1 ||
    policy.version > 0xffffffff ||
    !Array.isArray(policy.providers) ||
    policy.providers.length === 0 ||
    policy.providers.length > 3 ||
    policy.providers.at(-1) !== 'cpu'
  ) {
    throw adapterError('package_load_failed', 'Native runtime descriptor Auto policy is invalid');
  }
  if (descriptor.qualificationOnly === descriptor.released) {
    throw adapterError(
      'package_load_failed',
      'Native runtime descriptor must be either released or qualification-only',
    );
  }
  if (descriptor.qualificationOnly && policy.providers.some((provider) => provider !== 'cpu')) {
    throw adapterError('package_load_failed', 'Qualification runtime cannot alter released Auto policy');
  }
  const addon = verifyArtifact(root, descriptor.addon, 'addon');
  if (
    !descriptor.providers ||
    typeof descriptor.providers !== 'object' ||
    Array.isArray(descriptor.providers) ||
    !descriptor.providers.cpu
  ) {
    throw adapterError('package_load_failed', 'Native runtime descriptor omits CPU provider');
  }
  const referenced = new Set([descriptor.addon.path]);
  let webgpuCompatibility;
  for (const [providerId, provider] of Object.entries(descriptor.providers)) {
    if (!provider || !Array.isArray(provider.artifacts) || provider.artifacts.length === 0) {
      throw adapterError('package_load_failed', `Provider ${providerId} has no artifacts`);
    }
    provider.artifacts.forEach((artifact, index) => {
      verifyArtifact(root, artifact, `providers.${providerId}.artifacts[${index}]`);
      referenced.add(artifact.path);
    });
    if (providerId === 'webgpu') {
      const compatibilityPath = verifyArtifact(
        root,
        provider.compatibilityManifest,
        'providers.webgpu.compatibilityManifest',
      );
      try {
        webgpuCompatibility = JSON.parse(fs.readFileSync(compatibilityPath, 'utf8'));
      } catch (cause) {
        throw adapterError(
          'package_load_failed',
          'Unable to read the WebGPU compatibility manifest',
          provider.compatibilityManifest.path,
          cause,
        );
      }
      if (!provider.artifacts.some((artifact) => artifact.path === provider.compatibilityManifest.path)) {
        throw adapterError(
          'package_load_failed',
          'WebGPU compatibility manifest is not part of provider artifacts',
        );
      }
    }
  }
  const nativeDirectory = path.join(root, 'native');
  const actualPayload = new Set();
  const inventory = (directory, relativeDirectory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const relative = `${relativeDirectory}/${entry.name}`;
      const filename = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        throw adapterError('package_load_failed', 'Native runtime payload contains a symlink', relative);
      }
      if (entry.isDirectory()) {
        inventory(filename, relative);
      } else if (entry.isFile() && relative !== 'native/runtime-descriptor.json') {
        actualPayload.add(relative);
      } else if (!entry.isFile()) {
        throw adapterError('package_load_failed', 'Native runtime payload is not a regular file', relative);
      }
    }
  };
  inventory(nativeDirectory, 'native');
  if (
    actualPayload.size !== referenced.size ||
    [...actualPayload].some((filename) => !referenced.has(filename))
  ) {
    throw adapterError('package_load_failed', 'Native runtime descriptor payload inventory mismatch');
  }
  const availableProviders = Object.keys(descriptor.providers);
  if (
    new Set(availableProviders).size !== availableProviders.length ||
    availableProviders.some((provider) => !['cpu', 'apple', 'webgpu'].includes(provider)) ||
    new Set(policy.providers).size !== policy.providers.length ||
    policy.providers.some((provider) => !availableProviders.includes(provider))
  ) {
    throw adapterError('package_load_failed', 'Native runtime descriptor provider policy is invalid');
  }
  const runtime = descriptor.runtime;
  const expectedRuntimes = {
    cpu: {
      kind: 'onnxruntime-cpu',
      version: '1.22.0',
      abi: 'onnxruntime-c-api-22',
    },
    webgpu: {
      kind: 'onnxruntime-monolithic-webgpu',
      version: '1.23.0',
      abi: 'onnxruntime-c-api-23',
    },
  };
  const expectedRuntime = runtime && expectedRuntimes[runtime.flavor];
  if (
    !runtime ||
    typeof runtime !== 'object' ||
    Array.isArray(runtime) ||
    !expectedRuntime ||
    runtime.kind !== expectedRuntime.kind ||
    runtime.version !== expectedRuntime.version ||
    runtime.abi !== expectedRuntime.abi
  ) {
    throw adapterError('package_load_failed', 'Native runtime descriptor ABI identity is invalid');
  }
  if (runtime.flavor === 'webgpu') {
    const provider = descriptor.providers.webgpu;
    if (!provider || !Array.isArray(provider.artifacts)) {
      throw adapterError(
        'package_load_failed',
        'WebGPU runtime descriptor omits the WebGPU provider',
      );
    }
    const runtimeArtifacts = provider.artifacts.filter(
      (artifact) => artifact.path !== provider.compatibilityManifest.path,
    );
    const expectedCompatibilityKeys = [
      'platformId',
      'provider',
      'qualificationId',
      'qualificationOnly',
      'released',
      'runtimeAbi',
      'runtimeArtifact',
      'runtimeVersion',
      'schemaVersion',
    ];
    if (
      runtimeArtifacts.length !== 1 ||
      !webgpuCompatibility ||
      typeof webgpuCompatibility !== 'object' ||
      Array.isArray(webgpuCompatibility) ||
      Object.keys(webgpuCompatibility).sort().join('\0') !==
        expectedCompatibilityKeys.sort().join('\0') ||
      webgpuCompatibility.schemaVersion !== '1.0' ||
      webgpuCompatibility.provider !== 'webgpu' ||
      webgpuCompatibility.platformId !== actual.id ||
      webgpuCompatibility.runtimeVersion !== runtime.version ||
      webgpuCompatibility.runtimeAbi !== runtime.abi ||
      webgpuCompatibility.qualificationId !== provider.qualificationId ||
      webgpuCompatibility.qualificationOnly !== descriptor.qualificationOnly ||
      webgpuCompatibility.released !== descriptor.released ||
      !webgpuCompatibility.runtimeArtifact ||
      typeof webgpuCompatibility.runtimeArtifact !== 'object' ||
      Array.isArray(webgpuCompatibility.runtimeArtifact) ||
      Object.keys(webgpuCompatibility.runtimeArtifact).sort().join('\0') !==
        ['bytes', 'sha256'].join('\0') ||
      webgpuCompatibility.runtimeArtifact.bytes !== runtimeArtifacts[0].bytes ||
      webgpuCompatibility.runtimeArtifact.sha256 !== runtimeArtifacts[0].sha256
    ) {
      throw adapterError(
        'package_load_failed',
        'WebGPU compatibility manifest does not match the staged runtime contract',
      );
    }
  } else if (webgpuCompatibility !== undefined) {
    throw adapterError(
      'package_load_failed',
      'CPU runtime descriptor unexpectedly declares WebGPU compatibility',
    );
  }
  const expectedPolicy = descriptor.qualificationOnly
    ? ['cpu']
    : actual.os === 'darwin'
      ? ['apple', 'cpu']
      : runtime.flavor === 'webgpu'
        ? ['webgpu', 'cpu']
        : ['cpu'];
  const expectedAvailable = runtime.flavor === 'webgpu'
    ? ['cpu', 'webgpu']
    : actual.os === 'darwin'
      ? ['apple', 'cpu']
      : ['cpu'];
  const sortedAvailable = [...availableProviders].sort();
  const providerQualificationIds = sortedAvailable.map((providerId) => {
    const qualificationId = descriptor.providers[providerId]?.qualificationId;
    if (typeof qualificationId !== 'string' || qualificationId === '') {
      throw adapterError(
        'package_load_failed',
        `Provider ${providerId} has no qualification identity`,
      );
    }
    return qualificationId;
  });
  if (
    policy.providers.length !== expectedPolicy.length ||
    policy.providers.some((provider, index) => provider !== expectedPolicy[index]) ||
    sortedAvailable.length !== expectedAvailable.length ||
    sortedAvailable.some((provider, index) => provider !== expectedAvailable[index])
  ) {
    throw adapterError(
      'package_load_failed',
      'Native runtime descriptor providers disagree with platform capabilities',
    );
  }
  const runtimePolicy = Object.freeze({
    id: policy.id,
    version: policy.version,
    platformId: actual.id,
    runtimeFlavor: runtime.flavor,
    runtimeVersion: runtime.version,
    runtimeAbi: runtime.abi,
    qualificationOnly: descriptor.qualificationOnly,
    released: descriptor.released,
    orderedCandidates: Object.freeze([...policy.providers]),
    availableProviders: Object.freeze(sortedAvailable),
    providerQualificationIds: Object.freeze(providerQualificationIds),
  });
  return {
    addon,
    descriptor: Object.freeze(descriptor),
    descriptorPath: absoluteDescriptor,
    runtimePolicy,
  };
}

function resolveDevelopmentInput() {
  if (!process.env.LIGHT_OCR_NODE_BINARY) return undefined;
  const binary = path.resolve(process.env.LIGHT_OCR_NODE_BINARY);
  const descriptor = process.env.LIGHT_OCR_RUNTIME_DESCRIPTOR
    ? path.resolve(process.env.LIGHT_OCR_RUNTIME_DESCRIPTOR)
    : path.join(path.dirname(binary), 'runtime-descriptor.json');
  return { binary, descriptor };
}

function validateNativeContract(binding, runtimePolicy) {
  const contract = binding?.runtimeContract;
  const fields = [
    'policyId',
    'policyVersion',
    'platformId',
    'runtimeFlavor',
    'runtimeVersion',
    'runtimeAbi',
    'qualificationOnly',
    'released',
  ];
  const policyFields = {
    policyId: runtimePolicy.id,
    policyVersion: runtimePolicy.version,
    platformId: runtimePolicy.platformId,
    runtimeFlavor: runtimePolicy.runtimeFlavor,
    runtimeVersion: runtimePolicy.runtimeVersion,
    runtimeAbi: runtimePolicy.runtimeAbi,
    qualificationOnly: runtimePolicy.qualificationOnly,
    released: runtimePolicy.released,
  };
  if (
    !contract ||
    typeof contract !== 'object' ||
    fields.some((field) => contract[field] !== policyFields[field]) ||
    !Array.isArray(contract.orderedCandidates) ||
    !Array.isArray(contract.availableProviders) ||
    !Array.isArray(contract.providerQualificationIds) ||
    contract.orderedCandidates.length !== runtimePolicy.orderedCandidates.length ||
    contract.orderedCandidates.some(
      (provider, index) => provider !== runtimePolicy.orderedCandidates[index],
    ) ||
    contract.availableProviders.length !== runtimePolicy.availableProviders.length ||
    contract.availableProviders.some(
      (provider, index) => provider !== runtimePolicy.availableProviders[index],
    ) ||
    contract.providerQualificationIds.length !==
      runtimePolicy.providerQualificationIds.length ||
    contract.providerQualificationIds.some(
      (qualificationId, index) =>
        qualificationId !== runtimePolicy.providerQualificationIds[index],
    )
  ) {
    throw adapterError(
      'package_load_failed',
      'Runtime descriptor is incompatible with the native addon ABI or capabilities',
    );
  }
}

function loadNative() {
  const development = resolveDevelopmentInput();
  let input;
  if (development) {
    input = development;
  } else {
    const packageName = platformPackage();
    try {
      const binary = require.resolve(packageName);
      input = { binary, descriptor: path.join(path.dirname(binary), 'runtime-descriptor.json') };
    } catch (cause) {
      throw adapterError(
        'package_load_failed',
        `Unable to locate ${packageName}`,
        'Reinstall @arcships/light-ocr without --omit=optional and verify that the current platform is supported.',
        cause,
      );
    }
  }

  if (!fs.existsSync(input.binary)) {
    throw adapterError('package_load_failed', 'Native addon is missing', input.binary);
  }
  const verified = validateRuntimeDescriptor(input.descriptor);
  if (path.resolve(input.binary) !== path.resolve(verified.addon)) {
    throw adapterError('package_load_failed', 'Runtime descriptor addon path mismatch', input.binary);
  }
  try {
    const binding = require(verified.addon);
    validateNativeContract(binding, verified.runtimePolicy);
    return Object.freeze({
      binding,
      runtimePolicy: verified.runtimePolicy,
    });
  } catch (cause) {
    throw adapterError('package_load_failed', 'Unable to load the verified native addon', '', cause);
  }
}

module.exports = { loadNative, validateRuntimeDescriptor };
