import type { YearIndex } from "./types.gen.ts";

export type Route = { page: "today" } | { page: "archive"; date: string | null };

export const isoDay = /^\d{4}-\d{2}-\d{2}$/;

export function readRoute(hash: string): Route {
  if (hash === "#archive") return { page: "archive", date: null };
  if (hash.startsWith("#archive/")) {
    const date = hash.slice("#archive/".length);
    return { page: "archive", date: isoDay.test(date) ? date : null };
  }
  return { page: "today" };
}

export function archiveYears(files: Record<string, string>): string[] {
  return Object.keys(files).flatMap((path) => {
    const match = /^index\/(\d{4})\.json$/.exec(path);
    return match ? [match[1]] : [];
  }).sort().reverse();
}

const normalize = (value: string) => value.normalize("NFKC").toLowerCase();

export function filterArchive(index: YearIndex, query: string, category: string, tag: string) {
  const terms = normalize(query).trim().split(/\s+/).filter(Boolean);
  return Object.entries(index)
    .filter(([day, entry]) => {
      if (category && !entry.categories.includes(category)) return false;
      if (tag && !entry.tags.includes(tag)) return false;
      const corpus = normalize([
        day, entry.headline_ko, ...entry.categories, ...entry.tags, ...(entry.titles ?? []),
      ].join(" "));
      return terms.every((term) => corpus.includes(term));
    })
    .sort(([first], [second]) => second.localeCompare(first));
}
