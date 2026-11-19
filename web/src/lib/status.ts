import type { Manifest } from "./types.gen.ts";

const seoulClock = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Seoul",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function briefingStatus(manifest: Manifest, now: Date): "ok" | "failed" | "stale" {
  const parts = Object.fromEntries(seoulClock.formatToParts(now).map(({ type, value }) => [type, value]));
  const today = `${parts.year}-${parts.month}-${parts.day}`;
  const yesterday = new Date(Date.parse(today) - 86_400_000).toISOString().slice(0, 10);
  const expected = `${parts.hour}:${parts.minute}` >= (manifest.deadline_kst ?? "07:00") ? today : yesterday;

  if (manifest.last_run.date === expected && manifest.last_run.outcome === "failed") return "failed";
  if (manifest.published && manifest.published.date >= expected) return "ok";
  return "stale";
}
