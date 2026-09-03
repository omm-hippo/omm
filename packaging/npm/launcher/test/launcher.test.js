"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { PassThrough } = require("node:stream");
const test = require("node:test");

const launcherManifest = require("../package.json");
const {
  LauncherError,
  ignoreBrokenPipe,
  resolvePlatformPackage,
  run,
  selectTarget,
  signalExitCode,
} = require("../lib/launcher.js");

const BIN = path.join(__dirname, "..", "bin", "omm.js");

function tempDir(t, prefix = "omm-npm-launcher-") {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

function collector() {
  let output = "";
  return {
    write: (chunk) => (output += chunk),
    get text() {
      return output;
    },
  };
}

function fixture(t, overrides = {}, options = {}) {
  const content = options.content === undefined ? "standalone omm" : options.content;
  const root = tempDir(t);
  const binary = path.join(root, "bin", "omm");
  fs.mkdirSync(path.dirname(binary), { recursive: true });
  fs.writeFileSync(binary, content);
  fs.chmodSync(binary, options.mode === undefined ? 0o755 : options.mode);
  const manifest = {
    name: "@omm-hippo/omm-darwin-arm64",
    version: launcherManifest.version,
    os: ["darwin"],
    cpu: ["arm64"],
    omm: {
      distribution: "omm-model",
      target: "darwin-arm64",
      binary: "bin/omm",
      sha256: crypto.createHash("sha256").update(content).digest("hex"),
    },
    ...overrides,
  };
  const manifestPath = path.join(root, "package.json");
  fs.writeFileSync(
    manifestPath,
    options.manifestText === undefined ? JSON.stringify(manifest) : options.manifestText,
  );
  const realRoot = fs.realpathSync(root);
  return {
    root: realRoot,
    rawRoot: root,
    binary: fs.realpathSync(binary),
    manifestPath,
    resolvePackage: () => manifestPath,
  };
}

function resolveFixture(installed, extra = {}) {
  return resolvePlatformPackage({
    platform: "darwin",
    arch: "arm64",
    resolvePackage: installed.resolvePackage,
    ...extra,
  });
}

function runFixture(installed, extra = {}) {
  return run([], {
    platform: "darwin",
    arch: "arm64",
    resolvePackage: installed.resolvePackage,
    ...extra,
  });
}

function trySymlink(t, target, linkPath, type) {
  try {
    fs.symlinkSync(target, linkPath, type);
    return true;
  } catch (error) {
    t.skip(`this host cannot create ${type} symlinks: ${error.code || error.message}`);
    return false;
  }
}

test("selectTarget maps supported LTS targets and rejects unsupported libc", () => {
  assert.equal(selectTarget("darwin", "arm64").package, "@omm-hippo/omm-darwin-arm64");
  assert.equal(
    selectTarget("linux", "x64", {
      getReport: () => ({ header: { glibcVersionRuntime: "2.39" } }),
    }).package,
    "@omm-hippo/omm-linux-x64-gnu",
  );
  assert.throws(
    () => selectTarget("linux", "x64", { getReport: () => ({ header: {} }) }),
    /glibc Linux only/,
  );
  assert.throws(() => selectTarget("freebsd", "x64"), /no npm binary package/);
});

test("the current runner architecture has a declared target", (t) => {
  let selected;
  try {
    selected = selectTarget();
  } catch (error) {
    if (error instanceof LauncherError) {
      t.skip(`this host is not a published npm target: ${error.message}`);
      return;
    }
    throw error;
  }
  assert.equal(selected.os, process.platform);
  assert.equal(selected.cpu, process.arch);
});

test("resolvePlatformPackage verifies exact identity and binary containment", (t) => {
  const installed = fixture(t);
  const resolved = resolveFixture(installed);

  assert.equal(resolved.binary, installed.binary);
  assert.equal(resolved.root, installed.root);
});

test("resolvePlatformPackage rejects version and target mismatches", (t) => {
  const wrongVersion = fixture(t, { version: "9.9.9" });
  assert.throws(() => resolveFixture(wrongVersion), /metadata does not match/);

  const wrongTarget = fixture(t, {
    omm: { distribution: "omm-model", target: "darwin-x64", binary: "bin/omm" },
  });
  assert.throws(() => resolveFixture(wrongTarget), /metadata does not match/);
});

test("resolvePlatformPackage rejects an absent or non-hex declared checksum", (t) => {
  const missingChecksum = fixture(t, {
    omm: { distribution: "omm-model", target: "darwin-arm64", binary: "bin/omm" },
  });
  assert.throws(() => resolveFixture(missingChecksum), /metadata does not match/);

  const nonHexChecksum = fixture(t, {
    omm: {
      distribution: "omm-model",
      target: "darwin-arm64",
      binary: "bin/omm",
      sha256: "z".repeat(64),
    },
  });
  assert.throws(() => resolveFixture(nonHexChecksum), /metadata does not match/);

  const shortChecksum = fixture(t, {
    omm: {
      distribution: "omm-model",
      target: "darwin-arm64",
      binary: "bin/omm",
      sha256: "abc123",
    },
  });
  assert.throws(() => resolveFixture(shortChecksum), /metadata does not match/);
});

test("resolvePlatformPackage rejects a binary changed after packaging", (t) => {
  const installed = fixture(t);
  fs.writeFileSync(installed.binary, "tampered executable");

  assert.throws(() => resolveFixture(installed), /checksum is invalid/);
});

test("resolvePlatformPackage rejects a symlinked binary", (t) => {
  const installed = fixture(t);
  const payload = path.join(installed.rawRoot, "payload");
  fs.writeFileSync(payload, "standalone omm");
  fs.rmSync(installed.binary, { force: true });
  if (!trySymlink(t, payload, path.join(installed.rawRoot, "bin", "omm"), "file")) {
    return;
  }

  assert.throws(() => resolveFixture(installed), /must be a regular file/);
});

test("resolvePlatformPackage rejects a bin directory symlinked outside the package root", (t) => {
  const installed = fixture(t);
  const outside = tempDir(t, "omm-npm-outside-");
  fs.writeFileSync(path.join(outside, "omm"), "standalone omm");
  const binDir = path.join(installed.rawRoot, "bin");
  fs.rmSync(binDir, { recursive: true, force: true });
  const type = process.platform === "win32" ? "junction" : "dir";
  if (!trySymlink(t, outside, binDir, type)) {
    return;
  }

  assert.throws(() => resolveFixture(installed), /escapes its package root/);
});

test("run forwards argv and verified npm ownership metadata without a shell", (t) => {
  const installed = fixture(t);
  const calls = [];
  const status = run(["update", "--json"], {
    platform: "darwin",
    arch: "arm64",
    resolvePackage: installed.resolvePackage,
    spawnCommand(binary, argv, options) {
      calls.push({ binary, argv, options });
      return { status: 7 };
    },
  });

  assert.equal(status, 7);
  assert.equal(calls[0].binary, installed.binary);
  assert.deepEqual(calls[0].argv, ["update", "--json"]);
  assert.equal(calls[0].options.env.OMM_NPM_PACKAGE_ROOT, installed.root);
  assert.equal(calls[0].options.env.OMM_NPM_LAUNCHER_PACKAGE, "@omm-hippo/omm");
  assert.equal(calls[0].options.shell, undefined);
});

test("run reports a signal-terminated child as 128 plus the signal number", (t) => {
  const installed = fixture(t);
  const crashed = collector();
  const status = runFixture(installed, {
    stderr: crashed,
    spawnCommand: () => ({ status: null, signal: "SIGSEGV" }),
  });

  assert.equal(status, signalExitCode("SIGSEGV"));
  assert.equal(status, 128 + os.constants.signals.SIGSEGV);
  assert.match(crashed.text, /terminated by SIGSEGV/);

  const quiet = collector();
  assert.equal(
    runFixture(installed, {
      stderr: quiet,
      spawnCommand: () => ({ status: null, signal: "SIGINT" }),
    }),
    128 + os.constants.signals.SIGINT,
  );
  assert.equal(quiet.text, "");

  const unknown = collector();
  assert.equal(
    runFixture(installed, {
      stderr: unknown,
      spawnCommand: () => ({ status: null, signal: "SIGNOTREAL" }),
    }),
    1,
  );
});

test("run returns 143 when the real child kills itself with SIGTERM", (t) => {
  if (process.platform === "win32") {
    t.skip("Windows emulates signals, so a self-terminating child cannot be observed");
    return;
  }
  const installed = fixture(t, {}, { content: "#!/bin/sh\nkill -TERM $$\n" });
  const stderr = collector();
  const status = runFixture(installed, { stderr });

  assert.equal(status, 143);
  assert.match(stderr.text, /terminated by SIGTERM/);
});

test("run reports a spawn failure instead of an undefined status", (t) => {
  const installed = fixture(t, {}, { mode: 0o644 });
  const stderr = collector();
  const status = runFixture(installed, { stderr });

  assert.equal(status, 1);
  assert.match(stderr.text, /cannot start the @omm-hippo\/omm-darwin-arm64 executable/);
  assert.match(stderr.text, /npm install --global @omm-hippo\/omm/);
});

test("missing optional dependency fails with a recovery command", () => {
  const stderr = collector();
  const status = run([], {
    platform: "darwin",
    arch: "arm64",
    resolvePackage() {
      const error = new Error("Cannot find module '@omm-hippo/omm-darwin-arm64/package.json'");
      error.code = "MODULE_NOT_FOUND";
      throw error;
    },
    stderr,
  });

  assert.equal(status, 1);
  assert.match(stderr.text, /without --omit=optional/);
  assert.match(stderr.text, /@omm-hippo\/omm/);
});

test("a present but broken platform package is not reported as missing", (t) => {
  const brokenJson = fixture(t, {}, { manifestText: "{ not json" });
  const unreadable = collector();
  assert.equal(runFixture(brokenJson, { stderr: unreadable }), 1);
  assert.match(
    unreadable.text,
    /@omm-hippo\/omm-darwin-arm64 is installed but its package\.json is unreadable/,
  );
  assert.doesNotMatch(unreadable.text, /is missing/);
  assert.doesNotMatch(unreadable.text, /--omit=optional/);

  const blocked = collector();
  assert.equal(
    run([], {
      platform: "darwin",
      arch: "arm64",
      resolvePackage() {
        const error = new Error('Package subpath "./package.json" is not defined by "exports"');
        error.code = "ERR_PACKAGE_PATH_NOT_EXPORTED";
        throw error;
      },
      stderr: blocked,
    }),
    1,
  );
  assert.match(blocked.text, /installed but does not expose its package\.json/);
  assert.doesNotMatch(blocked.text, /--omit=optional/);

  const denied = collector();
  assert.equal(
    run([], {
      platform: "darwin",
      arch: "arm64",
      resolvePackage() {
        const error = new Error("EACCES: permission denied");
        error.code = "EACCES";
        throw error;
      },
      stderr: denied,
    }),
    1,
  );
  assert.match(denied.text, /Cannot load the npm package @omm-hippo\/omm-darwin-arm64: EACCES/);
  assert.doesNotMatch(denied.text, /--omit=optional/);
});

test("ignoreBrokenPipe swallows EPIPE and flags other stream failures", (t) => {
  const previous = process.exitCode;
  t.after(() => {
    process.exitCode = previous;
  });

  const stream = ignoreBrokenPipe(new PassThrough());
  const epipe = new Error("write EPIPE");
  epipe.code = "EPIPE";
  stream.emit("error", epipe);
  assert.equal(process.exitCode, previous);

  const other = new Error("write EIO");
  other.code = "EIO";
  stream.emit("error", other);
  assert.equal(process.exitCode, 1);
});

function fakePlatformPackage(t) {
  let target;
  try {
    target = selectTarget();
  } catch (error) {
    if (error instanceof LauncherError) {
      t.skip(`this host is not a published npm target: ${error.message}`);
      return null;
    }
    throw error;
  }
  const root = tempDir(t, "omm-npm-nodepath-");
  const packageRoot = path.join(root, "node_modules", ...target.package.split("/"));
  fs.mkdirSync(packageRoot, { recursive: true });
  fs.writeFileSync(
    path.join(packageRoot, "package.json"),
    JSON.stringify({ name: target.package, version: "9.9.9" }),
  );
  return { nodePath: path.join(root, "node_modules"), target };
}

function runBin(nodePath) {
  return spawn(process.execPath, [BIN, "--version"], {
    env: { ...process.env, NODE_PATH: nodePath },
    stdio: ["ignore", "ignore", "pipe"],
  });
}

test("bin/omm.js reports a broken platform package on one line", async (t) => {
  const fake = fakePlatformPackage(t);
  if (!fake) return;

  const child = runBin(fake.nodePath);
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => (stderr += chunk));
  const [code] = await once(child, "exit");

  assert.equal(code, 1);
  assert.match(stderr, /^omm: .*metadata does not match/);
  assert.doesNotMatch(stderr, /\n\s+at /);
  assert.equal(stderr.trimEnd().split("\n").length, 1);
});

test("bin/omm.js exits 1 without a stack trace when stderr is closed", async (t) => {
  const fake = fakePlatformPackage(t);
  if (!fake) return;

  const child = runBin(fake.nodePath);
  child.stderr.destroy();
  const [code, signal] = await once(child, "exit");

  assert.equal(signal, null);
  assert.equal(code, 1);
});

test("package has exact platform versions and no install lifecycle script", () => {
  const expectedNames = [
    "@omm-hippo/omm-darwin-arm64",
    "@omm-hippo/omm-darwin-x64",
    "@omm-hippo/omm-linux-arm64-gnu",
    "@omm-hippo/omm-linux-x64-gnu",
    "@omm-hippo/omm-win32-x64",
  ];
  assert.deepEqual(Object.keys(launcherManifest.optionalDependencies).sort(), expectedNames);
  assert.ok(
    Object.values(launcherManifest.optionalDependencies).every(
      (version) => version === launcherManifest.version,
    ),
  );
  for (const name of ["preinstall", "install", "postinstall", "prepare"]) {
    assert.equal(launcherManifest.scripts[name], undefined);
  }
});
