/** Generate synthetic source SDK -> bridge health manifests without command or network I/O. */
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const sdk = new URL("../../../eufy-sdk/", import.meta.url);
const bridge = new URL("../../../ha-eufy-sdk-bridge/", import.meta.url);
const require = createRequire(new URL("package.json", sdk));
const { build } = require("esbuild");
const { format } = require("prettier");
const output = new URL("../../.offline/doorbell-health/", import.meta.url);
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
const { createDeviceView } = await import(
  new URL("src/device-view.mjs", bridge)
);
const { createState } = await import(new URL("src/state.mjs", bridge));
const view = createDeviceView({ eufy: {}, state: createState() });
const cases = [];
for (const [model, health] of [
  ["T8214", 0],
  ["T8214", null],
  ["T8217", 0],
  ["T8134", 0],
  ["T8134", 89],
]) {
  const dev = Device.fromRecord("synthetic-device", {
    model,
    parentSn: "synthetic-station",
    params: {
      1101: "80",
      ...(health === null ? {} : { 1198: String(health) }),
    },
  });
  dev.setStationContext({ stationModel: "T9000" });
  dev.bindActions(
    {
      model,
      codec: dev.codec,
      channel: 2,
      stationModel: "T9000",
      paramIds: new Set([1101, ...(health === null ? [] : [1198])]),
    },
    {
      dispatch: async () => {
        throw new Error("Command I/O forbidden");
      },
    },
  );
  cases.push({
    model,
    health,
    reply: {
      properties: view
        .propertySpecs(dev)
        .filter((p) => ["battery", "batteryHealth"].includes(p.name)),
      decodedProperties: {
        bound: true,
        details: view
          .decodedProperties(dev)
          .details.filter((detail) => detail.capability === "battery"),
      },
    },
  });
}
const target = new URL("doorbell_health_manifest.json", import.meta.url);
const json = await format(JSON.stringify({ cases }), { parser: "json" });
if (process.argv.includes("--check")) {
  if (readFileSync(target, "utf8") !== json)
    throw new Error("Doorbell health fixture differs from current source");
} else writeFileSync(target, json);
console.log(
  `Verified ${cases.length} source SDK -> bridge health cases; command/network I/O forbidden`,
);
