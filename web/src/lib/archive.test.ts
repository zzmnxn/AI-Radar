import assert from "node:assert/strict";
import test from "node:test";
import { archiveYears, filterArchive, readRoute } from "./archive.ts";
import type { YearIndex } from "./types.gen.ts";

const index = {
  "2026-11-04": {
    path: "briefings/2026-11-04.json", headline_ko: "문서 도구 소식", card_count: 2,
    categories: ["개발 도구"], tags: ["검색", "LLM"], titles: ["Vector 검색 배포"],
  },
  "2026-11-05": {
    path: "briefings/2026-11-05.json", headline_ko: "모델 공개", card_count: 1,
    categories: ["연구"], tags: ["LLM"], titles: ["경량 모델"],
  },
} satisfies YearIndex;

test("archive filters combine category, tag and normalized Korean and English search", () => {
  assert.deepEqual(filterArchive(index, "ＶＥＣＴＯＲ 검색", "개발 도구", "LLM").map(([day]) => day), ["2026-11-04"]);
  assert.deepEqual(filterArchive(index, "벡터", "", ""), []);
  assert.deepEqual(filterArchive(index, "", "연구", "검색"), []);
  assert.deepEqual(filterArchive(index, "", "", "LLM").map(([day]) => day), ["2026-11-05", "2026-11-04"]);
});

test("archives are loaded only for listed years and accept date routes", () => {
  assert.deepEqual(archiveYears({ "index/2024.json": "", "index/2026.json": "", "briefings/2025-01-01.json": "" }), ["2026", "2024"]);
  assert.deepEqual(readRoute("#archive/2026-11-05"), { page: "archive", date: "2026-11-05" });
  assert.deepEqual(readRoute("#archive/../../index"), { page: "archive", date: null });
  assert.deepEqual(readRoute(""), { page: "today" });
});
