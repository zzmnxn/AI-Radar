import { isoDay } from "./archive.ts";
import { SCHEMA_VERSION, type Briefing, type Manifest, type YearIndex } from "./types.gen.ts";

export interface Snapshot {
  manifest: Manifest;
  build: { commit: string; built_at: string } | null;
}

const base = import.meta.env?.BASE_URL ?? "/";
const headers = { Accept: "application/json" };

export class SchemaVersionError extends Error {}

function verifyVersion(version: string | undefined) {
  if (version?.split(".")[0] !== SCHEMA_VERSION.split(".")[0]) throw new SchemaVersionError();
}

export async function loadSnapshot(signal: AbortSignal): Promise<Snapshot | null> {
  const response = await fetch(`${base}data/manifest.json`, { cache: "no-store", headers, signal });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("Manifest unavailable");
  const manifest: Manifest = await response.json();
  verifyVersion(manifest.schema_version);

  const buildResponse = await fetch(`${base}build-info.json`, { headers, signal }).catch(() => null);
  const build = buildResponse?.ok ? await buildResponse.json().catch(() => null) : null;
  return { manifest, build };
}

async function readPublished<T>(manifest: Manifest, path: string, signal: AbortSignal): Promise<T> {
  const digest = manifest.published?.files[path];
  if (!digest) throw new Error("File missing from manifest");
  const response = await fetch(`${base}data/${path}`, { cache: "no-store", headers, signal });
  if (!response.ok) throw new Error("Snapshot file unavailable");
  const contents = await response.arrayBuffer();
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", contents))
    .reduce((hex, byte) => hex + byte.toString(16).padStart(2, "0"), "");
  if (hash !== digest) throw new Error("Snapshot checksum mismatch");
  return JSON.parse(new TextDecoder().decode(contents)) as T;
}

export async function loadBriefing(manifest: Manifest, day: string, signal: AbortSignal) {
  if (!isoDay.test(day)) throw new Error("Invalid briefing date");
  const briefing = await readPublished<Briefing>(manifest, `briefings/${day}.json`, signal);
  verifyVersion(briefing.schema_version);
  if (
    briefing.date !== day ||
    (day === manifest.published?.date && briefing.run_id !== manifest.published.run_id)
  ) throw new Error("Snapshot mismatch");
  return briefing;
}

export async function loadArchive(manifest: Manifest, year: string, signal: AbortSignal) {
  if (!/^\d{4}$/.test(year)) throw new Error("Invalid archive year");
  const index = await readPublished<YearIndex>(manifest, `index/${year}.json`, signal);
  for (const [day, entry] of Object.entries(index)) {
    if (!isoDay.test(day) || day.slice(0, 4) !== year ||
        entry.path !== `briefings/${day}.json` || !manifest.published?.files[entry.path]) {
      throw new Error("Invalid archive entry");
    }
  }
  return index;
}
