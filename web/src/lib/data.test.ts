import assert from "node:assert/strict";
import test from "node:test";
import { loadArchive, loadBriefing, loadSnapshot, SchemaVersionError } from "./data.ts";
import type { Manifest } from "./types.gen.ts";

test("manifest is read first and an incompatible major prevents data reads", async (t) => {
  const paths: string[] = [];
  t.mock.method(globalThis, "fetch", async (path, options) => {
    paths.push(String(path));
    assert.equal(options.cache, "no-store");
    return Response.json({ schema_version: "999.0.0" });
  });
  await assert.rejects(loadSnapshot(new AbortController().signal), SchemaVersionError);
  assert.deepEqual(paths, ["/data/manifest.json"]);
});

test("only the requested listed index and briefing are read and hashes are checked", async (t) => {
  const day = "2026-11-05";
  const index = { [day]: { path: `briefings/${day}.json`, headline_ko: "제목", card_count: 0, categories: [], tags: [] } };
  const briefing = { schema_version: "1.0.0", date: day, run_id: "run" };
  const fixtures: Record<string, string> = {
    "/data/index/2026.json": JSON.stringify(index),
    [`/data/briefings/${day}.json`]: JSON.stringify(briefing),
  };
  const files: Record<string, string> = {};
  for (const [path, body] of Object.entries(fixtures)) {
    files[path.slice(6)] = Buffer.from(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body))).toString("hex");
  }
  const calls: string[] = [];
  t.mock.method(globalThis, "fetch", async (path) => {
    calls.push(String(path));
    return new Response(fixtures[String(path)]);
  });
  const manifest = { published: { date: day, run_id: "run", files } } as Manifest;
  const signal = new AbortController().signal;
  await assert.rejects(loadArchive(manifest, "2025", signal));
  assert.deepEqual(calls, []);
  assert.deepEqual(await loadArchive(manifest, "2026", signal), index);
  assert.deepEqual(await loadBriefing(manifest, day, signal), briefing);
  assert.deepEqual(calls, ["/data/index/2026.json", `/data/briefings/${day}.json`]);
  fixtures["/data/index/2026.json"] = "{}";
  await assert.rejects(loadArchive(manifest, "2026", signal), /checksum/);
});
