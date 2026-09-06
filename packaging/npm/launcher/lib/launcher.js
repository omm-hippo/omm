"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");

const launcherManifest = require("../package.json");
const targets = require("../targets.json");

const REINSTALL = "Reinstall with: npm install --global @omm-hippo/omm";
const QUIET_SIGNALS = new Set(["SIGINT", "SIGPIPE"]);

class LauncherError extends Error {}

function describe(error) {
  const message = error && error.message ? error.message : String(error);
  return message.replace(/\.+$/, "");
}

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

function isBrokenPipe(error) {
  return Boolean(error) && (error.code === "EPIPE" || error.code === "ERR_STREAM_DESTROYED");
}

function ignoreBrokenPipe(stream) {
  stream.on("error", (error) => {
    if (isBrokenPipe(error)) {
      return;
    }
    process.exitCode = 1;
  });
  return stream;
}

function readManifest(manifestPath, packageName) {
  try {
    return JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch (error) {
    throw new LauncherError(
      `The npm package ${packageName} is installed but its package.json is unreadable: ` +
        `${describe(error)}. ${REINSTALL}`,
    );
  }
}

function resolveManifestPath(resolvePackage, target) {
  try {
    return resolvePackage(`${target.package}/package.json`);
  } catch (error) {
    if (error && error.code === "MODULE_NOT_FOUND") {
      throw new LauncherError(
        `The optional npm package ${target.package}@${launcherManifest.version} is missing. ` +
          "Reinstall @omm-hippo/omm without --omit=optional and check that this platform is supported.",
      );
    }
    if (error && error.code === "ERR_PACKAGE_PATH_NOT_EXPORTED") {
      throw new LauncherError(
        `The npm package ${target.package} is installed but does not expose its package.json. ` +
          `${REINSTALL}`,
      );
    }
    throw new LauncherError(
      `Cannot load the npm package ${target.package}: ${describe(error)}. ${REINSTALL}`,
    );
  }
}

function exactArray(value, expected) {
  return Array.isArray(value) && value.length === 1 && value[0] === expected;
}

function sha256File(filePath) {
  const digest = crypto.createHash("sha256");
  const descriptor = fs.openSync(filePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const bytesRead = fs.readSync(descriptor, buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      digest.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    fs.closeSync(descriptor);
  }
  return digest.digest("hex");
}

function resolvePlatformPackage(options = {}) {
  const target = selectTarget(options.platform, options.arch, options.report);
  const resolvePackage = options.resolvePackage || require.resolve;
  const manifestPath = resolveManifestPath(resolvePackage, target);

  const root = fs.realpathSync(path.dirname(manifestPath));
  const manifest = readManifest(manifestPath, target.package);
  const metadata = manifest.omm;
  if (
    manifest.name !== target.package ||
    manifest.version !== launcherManifest.version ||
    !exactArray(manifest.os, target.os) ||
    !exactArray(manifest.cpu, target.cpu) ||
    !metadata ||
    metadata.distribution !== "omm-model" ||
    metadata.target !== target.key ||
    metadata.binary !== target.binary ||
    typeof metadata.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(metadata.sha256)
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
  if (sha256File(binary) !== metadata.sha256) {
    throw new LauncherError(`The ${target.package} executable checksum is invalid.`);
  }
  return { root, binary, manifest, target };
}

function signalExitCode(signal) {
  const number = os.constants.signals[signal];
  return Number.isInteger(number) ? 128 + number : 1;
}

function run(argv, options = {}) {
  const stderr = options.stderr || process.stderr;
  let resolved;
  try {
    resolved = resolvePlatformPackage(options);
  } catch (error) {
    const message = error instanceof LauncherError ? error.message : String(error);
    stderr.write(`omm: ${message}\n`);
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
    const reason = result.error.code || result.error.message;
    stderr.write(
      `omm: cannot start the ${resolved.target.package} executable: ${reason}. ${REINSTALL}\n`,
    );
    return 1;
  }
  if (result.signal) {
    if (!QUIET_SIGNALS.has(result.signal)) {
      stderr.write(`omm: the omm executable was terminated by ${result.signal}.\n`);
    }
    return signalExitCode(result.signal);
  }
  return Number.isInteger(result.status) ? result.status : 1;
}

module.exports = {
  LauncherError,
  ignoreBrokenPipe,
  isBrokenPipe,
  resolvePlatformPackage,
  run,
  runtimeLibc,
  selectTarget,
  sha256File,
  signalExitCode,
};
