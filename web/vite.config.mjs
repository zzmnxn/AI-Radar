import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const base = process.env.VITE_BASE || "/";
const location = process.env.RADAR_DATA || fileURLToPath(new URL("../data", import.meta.url));

function snapshotFiles() {
  const files = new Map();
  const manifestPath = resolve(location, "manifest.json");
  if (!existsSync(manifestPath)) return files;
  const manifest = readFileSync(manifestPath);
  files.set("data/manifest.json", manifest);
  const { published } = JSON.parse(manifest);
  for (const [name, expected] of Object.entries(published?.files ?? {})) {
    if (!/^(briefings\/\d{4}-\d{2}-\d{2}|index\/\d{4})\.json$/.test(name)) {
      throw new Error(`Unexpected published path: ${name}`);
    }
    const contents = readFileSync(resolve(location, name));
    if (createHash("sha256").update(contents).digest("hex") !== expected) {
      throw new Error(`Snapshot checksum mismatch: ${name}`);
    }
    files.set(`data/${name}`, contents);
  }
  return files;
}

export default defineConfig({
  base,
  plugins: [{
    name: "radar-snapshot",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const name = req.url?.split("?", 1)[0].slice(base.length);
        if (!req.url?.startsWith(`${base}data/`)) return next();
        try {
          const bytes = snapshotFiles().get(name);
          res.statusCode = bytes ? 200 : 404;
          res.setHeader("Content-Type", "application/json");
          res.end(bytes);
        } catch (error) {
          next(error);
        }
      });
    },
    generateBundle() {
      for (const [fileName, source] of snapshotFiles()) {
        this.emitFile({ type: "asset", fileName, source });
      }
      this.emitFile({
        type: "asset",
        fileName: "build-info.json",
        source: JSON.stringify({
          commit: process.env.RADAR_COMMIT ?? process.env.GITHUB_SHA ?? "local",
          built_at: new Date().toISOString(),
        }),
      });
    },
  }],
});
