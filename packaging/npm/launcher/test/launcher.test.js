"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const launcherManifest = require("../package.json");
const {
  LauncherError,
  resolvePlatformPackage,
  run,
  selectTarget,
} = require("../lib/launcher.js");

function fixture(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "omm-npm-launcher-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const binary = path.join(root, "bin", "omm");
  fs.mkdirSync(path.dirname(binary), { recursive: true });
  fs.writeFileSync(binary, "standalone omm");
  fs.chmodSync(binary, 0o755);
  const manifest = {
    name: "@omm-hippo/omm-darwin-arm64",
    version: launcherManifest.version,
    os: ["darwin"],
    cpu: ["arm64"],
    omm: {
      distribution: "omm-model",
      target: "darwin-arm64",
      binary: "bin/omm",
      sha256: crypto.createHash("sha256").update("standalone omm").digest("hex"),
    },
    ...overrides,
  };
  fs.writeFileSync(path.join(root, "package.json"), JSON.stringify(manifest));
  const realRoot = fs.realpathSync(root);
  return {
    root: realRoot,
    binary: fs.realpathSync(binary),
    resolvePackage: () => path.join(root, "package.json"),
  };
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

test("the current runner architecture has a declared target", () => {
  const selected = selectTarget();
  assert.equal(selected.os, process.platform);
  assert.equal(selected.cpu, process.arch);
});

test("resolvePlatformPackage verifies exact identity and binary containment", (t) => {
  const installed = fixture(t);
  const resolved = resolvePlatformPackage({
    platform: "darwin",
    arch: "arm64",
    resolvePackage: installed.resolvePackage,
  });

  assert.equal(resolved.binary, installed.binary);
  assert.equal(resolved.root, installed.root);
});

test("resolvePlatformPackage rejects version and target mismatches", (t) => {
  const wrongVersion = fixture(t, { version: "9.9.9" });
  assert.throws(
    () =>
      resolvePlatformPackage({
        platform: "darwin",
        arch: "arm64",
        resolvePackage: wrongVersion.resolvePackage,
      }),
    /metadata does not match/,
  );

  const wrongTarget = fixture(t, {
    omm: { distribution: "omm-model", target: "darwin-x64", binary: "bin/omm" },
  });
  assert.throws(
    () =>
      resolvePlatformPackage({
        platform: "darwin",
        arch: "arm64",
        resolvePackage: wrongTarget.resolvePackage,
      }),
    /metadata does not match/,
  );
});

test("resolvePlatformPackage rejects a binary changed after packaging", (t) => {
  const installed = fixture(t);
  fs.writeFileSync(installed.binary, "tampered executable");

  assert.throws(
    () =>
      resolvePlatformPackage({
        platform: "darwin",
        arch: "arm64",
        resolvePackage: installed.resolvePackage,
      }),
    /checksum is invalid/,
  );
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

test("missing optional dependency fails with a recovery command", () => {
  let output = "";
  const status = run([], {
    platform: "darwin",
    arch: "arm64",
    resolvePackage() {
      throw new Error("missing");
    },
    stderr: { write: (chunk) => (output += chunk) },
  });

  assert.equal(status, 1);
  assert.match(output, /without --omit=optional/);
  assert.match(output, /@omm-hippo\/omm/);
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
