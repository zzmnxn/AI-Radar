import assert from "node:assert/strict";
import test from "node:test";
import type { Manifest } from "./types.gen.ts";
import { briefingStatus } from "./status.ts";

const manifest: Manifest = {
  deadline_kst: "07:00",
  published: {
    date: "2026-11-17",
    files: {},
    generated_at: "2026-11-16T21:30:00Z",
    run_id: "previous",
  },
  last_run: {
    date: "2026-11-18",
    started_at: "2026-11-17T21:00:00Z",
    finished_at: "2026-11-17T21:01:00Z",
    outcome: "ok",
    run_id: "next",
    sources: { active: 4, complete: 4, failed: 0, partial: 0 },
  },
};

test("KST cutoff switches expected date and reports missing publication", () => {
  assert.equal(briefingStatus(manifest, new Date("2026-11-17T21:59:00Z")), "ok");
  assert.equal(briefingStatus(manifest, new Date("2026-11-17T22:01:00Z")), "stale");
});

test("KST cutoff reports a failed run for the expected date", () => {
  const failed: Manifest = {
    ...manifest,
    last_run: { ...manifest.last_run, outcome: "failed" },
  };
  assert.equal(briefingStatus(failed, new Date("2026-11-17T22:01:00Z")), "failed");
});

test("a briefing published before cutoff is current", () => {
  const current = { ...manifest, published: { ...manifest.published!, date: "2026-11-18" } };
  assert.equal(briefingStatus(current, new Date("2026-11-17T21:59:00Z")), "ok");
});

test("a failed forced run retains the same-date briefing with a failure notice", () => {
  const failed: Manifest = {
    ...manifest,
    published: { ...manifest.published!, date: "2026-11-18" },
    last_run: { ...manifest.last_run, outcome: "failed" },
  };
  assert.equal(briefingStatus(failed, new Date("2026-11-17T22:01:00Z")), "failed");
});
