/** Regenerate synthetic SDK source -> bridge RPC manifests; all command and network I/O is forbidden. */
import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import http from "node:http";
import { fileURLToPath } from "node:url";

const sdk = new URL("../../../eufy-sdk/", import.meta.url);
const bridge = new URL("../../../ha-eufy-sdk-bridge/", import.meta.url);
const require = createRequire(new URL("package.json", sdk));
const { build } = require("esbuild");
const { format } = require("prettier");
const output = new URL("../../.offline/remediation/", import.meta.url);
mkdirSync(output, { recursive: true });
const bundle = await build({
  entryPoints: [fileURLToPath(new URL("src/model/device.ts", sdk))],
  bundle: true,
  platform: "node",
  format: "esm",
  packages: "external",
  write: false,
});
const bundled = new URL("device-source.mjs", output);
writeFileSync(bundled, bundle.outputFiles[0].contents);
await import(new URL("test/fixtures/no-socket-guard.mjs", bridge));
const { Device } = await import(bundled);
const { createWsServer } = await import(new URL("src/ws-server.mjs", bridge));
const { createDeviceView } = await import(
  new URL("src/device-view.mjs", bridge)
);
const { createState } = await import(new URL("src/state.mjs", bridge));
const sourceSha256 = {};
for (const [root, paths] of [
  [
    sdk,
    [
      "src/model/device.ts",
      "src/model/capabilities/light.ts",
      "src/model/capabilities/members.ts",
      "src/model/capabilities/manifest.ts",
    ],
  ],
  [
    bridge,
    [
      "src/device-view.mjs",
      "src/ws-server.mjs",
      "src/bounded-state-writes.mjs",
    ],
  ],
]) {
  for (const path of paths)
    sourceSha256[`${root === sdk ? "sdk" : "bridge"}/${path}`] = createHash(
      "sha256",
    )
      .update(readFileSync(new URL(path, root)))
      .digest("hex");
}
const cases = [];
for (const model of ["T8425", "T8426"]) {
  for (const parent of ["T9000", "T8030", "throw", "missing"]) {
    const deviceType = model === "T8425" ? 47 : 87;
    const camera = Device.fromRecord("synthetic-camera", {
      model,
      deviceType,
      parentSn: "synthetic-station",
      params: { 1400: "0", 1401: "50", 1403: "1" },
    });
    camera.setStationContext({
      stationModel: parent === "T8030" ? parent : "T9000",
    });
    camera.bindActions(
      {
        model,
        deviceType,
        codec: camera.codec,
        channel: 0,
        stationModel: parent === "T8030" ? parent : "T9000",
        homeBaseAttached: true,
        capabilities: new Set(camera.capabilities),
        paramIds: new Set([1400, 1401, 1403]),
      },
      {
        dispatch: async () => {
          throw new Error("Command I/O forbidden");
        },
      },
    );
    camera.writeOnlySettings = [
      { name: "colorTemp", type: "number", kind: "scalar", min: 0, max: 100 },
    ];
    const eufy = {
      getDevice: async (sn) => {
        if (sn === camera.sn) return camera;
        if (parent === "throw") throw new Error("Synthetic parent unavailable");
        if (parent === "missing") return undefined;
        return {
          sn: "synthetic-station",
          stationSn: "synthetic-station",
          model: parent,
        };
      },
    };
    const state = createState();
    state.flags.ready = true;
    const view = createDeviceView({ eufy, state });
    const ws = createWsServer(
      {
        cfg: {},
        state,
        eufy,
        dbg() {},
        propertySpecs: view.propertySpecs,
        decodedProperties: view.decodedProperties,
      },
      http.createServer(),
    );
    let reply;
    try {
      await ws.handleMessage(
        {
          OPEN: 1,
          readyState: 1,
          send: (raw) => {
            reply = JSON.parse(raw);
          },
        },
        Buffer.from(
          JSON.stringify({ id: 1, cmd: "device.properties", sn: camera.sn }),
        ),
      );
    } finally {
      await ws.closeWebSocketServer();
    }
    reply.properties = reply.properties?.filter((s) =>
      ["light", "brightness", "spotlightEnabled", "colorTemp"].includes(s.name),
    );
    if (reply.decodedProperties)
      reply.decodedProperties.details = reply.decodedProperties.details.filter(
        (d) => d.capability === "light",
      );
    cases.push({ model, parent, reply });
  }
}
const target = new URL("light_route_manifest.json", import.meta.url);
const json = await format(JSON.stringify({ sourceSha256, cases }), {
  parser: "json",
});
if (process.argv.includes("--check")) {
  if (readFileSync(target, "utf8") !== json)
    throw new Error("Light-route fixture differs from current source");
} else writeFileSync(target, json);
console.log(
  `Verified ${cases.length} synthetic source SDK -> bridge RPC cases; command/network I/O forbidden`,
);
