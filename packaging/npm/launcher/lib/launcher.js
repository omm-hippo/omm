"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const launcherManifest = require("../package.json");
const targets = require("../targets.json");

class LauncherError extends Error {}

function runtimeLibc(report = process.report) {
  if (!report || typeof report.getReport !== "function") {
    return null;
  }
  const header = report.getReport()?.header;
  return header?.glibcVersionRuntime ? "glibc" : null;
}

function selectTarget(
  platform = process.platform,
  arch = process.arch,
  report = process.report,
) {
  let key = `${platform}-${arch}`;
  if (platform === "linux") {
    const libc = runtimeLibc(report);
    if (libc !== "glibc") {
      throw new LauncherError(
        "OMM npm packages currently support glibc Linux only; musl and unknown libc systems are not yet supported.",
      );
    }
    key = `${key}-gnu`;
  }
  const target = targets[key];
  if (!target) {
    throw new LauncherError(
      `OMM has no npm binary package for ${platform}/${arch}. Supported targets: ${Object.keys(targets).join(", ")}`,
    );
  }
  return { key, ...target };
}

function readManifest(manifestPath) {
  try {
    return JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch (error) {
    throw new LauncherError(`Cannot read npm platform package metadata: ${error.message}`);
  }
}

function exactArray(value, expected) {
  return Array.isArray(value) && value.length === 1 && value[0] === expected;
}

function resolvePlatformPackage(options = {}) {
  const target = selectTarget(options.platform, options.arch, options.report);
  const resolvePackage = options.resolvePackage || require.resolve;
  let manifestPath;
  try {
    manifestPath = resolvePackage(`${target.package}/package.json`);
  } catch (error) {
    throw new LauncherError(
      `The optional npm package ${target.package}@${launcherManifest.version} is missing. ` +
        "Reinstall @omm-hippo/omm without --omit=optional and check that this platform is supported.",
    );
  }

  const root = fs.realpathSync(path.dirname(manifestPath));
  const manifest = readManifest(manifestPath);
  const metadata = manifest.omm;
  if (
    manifest.name !== target.package ||
    manifest.version !== launcherManifest.version ||
    !exactArray(manifest.os, target.os) ||
    !exactArray(manifest.cpu, target.cpu) ||
    !metadata ||
    metadata.distribution !== "omm-model" ||
    metadata.target !== target.key ||
    metadata.binary !== target.binary
  ) {
    throw new LauncherError(
      `The installed ${target.package} metadata does not match @omm-hippo/omm ${launcherManifest.version}.`,
    );
  }
  if (target.libc && !exactArray(manifest.libc, target.libc)) {
    throw new LauncherError(`The installed ${target.package} libc metadata is invalid.`);
  }

  const binaryPath = path.join(root, target.binary);
  const stat = fs.lstatSync(binaryPath);
  if (!stat.isFile() || stat.isSymbolicLink()) {
    throw new LauncherError(`The ${target.package} executable must be a regular file.`);
  }
  const binary = fs.realpathSync(binaryPath);
  const relative = path.relative(root, binary);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new LauncherError(`The ${target.package} executable escapes its package root.`);
  }
  return { root, binary, manifest, target };
}

function run(argv, options = {}) {
  let resolved;
  try {
    resolved = resolvePlatformPackage(options);
  } catch (error) {
    const message = error instanceof LauncherError ? error.message : String(error);
    (options.stderr || process.stderr).write(`omm: ${message}\n`);
    return 1;
  }

  const spawnCommand = options.spawnCommand || spawnSync;
  const result = spawnCommand(resolved.binary, argv, {
    stdio: "inherit",
    windowsHide: false,
    env: {
      ...process.env,
      OMM_NPM_PACKAGE_ROOT: resolved.root,
      OMM_NPM_LAUNCHER_PACKAGE: launcherManifest.name,
    },
  });
  if (result.error) {
    (options.stderr || process.stderr).write(`omm: ${result.error.message}\n`);
    return 1;
  }
  return Number.isInteger(result.status) ? result.status : 1;
}

module.exports = {
  LauncherError,
  resolvePlatformPackage,
  run,
  runtimeLibc,
  selectTarget,
};
