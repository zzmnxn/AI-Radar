import assert from "node:assert/strict";
import test from "node:test";
import { byImportance, layout, ringOf, spread } from "./radar.ts";

test("ringOf uses fixed score thresholds", () => {
  assert.deepEqual([88, 85, 84, 70, 69, 56].map(ringOf), [0, 0, 1, 1, 2, 2]);
});

test("byImportance sorts by score descending, stable on ties", () => {
  const cards = [{ id: "a", score: 59 }, { id: "b", score: 88 }, { id: "c", score: 59 }, { id: "d", score: 87 }];
  assert.deepEqual(byImportance(cards).map((card) => card.id), ["b", "d", "a", "c"]);
});

test("spread keeps neighbours apart and inside the arc", () => {
  const inArc = (angle: number) => angle >= Math.PI * 0.07 - 1e-9 && angle <= Math.PI * 0.93 + 1e-9;
  const fits = [...spread([3, 3, 3], 32, 22)].sort((a, b) => a - b);
  assert.ok(fits.every(inArc));
  for (let i = 1; i < fits.length; i++) assert.ok(fits[i] - fits[i - 1] >= 22 / 32 - 1e-9);
  // Too many for the arc: falls back to even spacing, still inside.
  const crowded = spread([1, 1, 1, 1, 1, 1], 32, 22);
  assert.ok(crowded.every(inArc));
  assert.equal(new Set(crowded).size, 6);
});

test("layout places blips in their ring band without overlap", () => {
  // 2026-11-20: two must-know cards, two watch cards in the same category.
  const cards = [
    { category: "AI 개발 워크플로", score: 88 }, { category: "AI 코딩 도구", score: 87 },
    { category: "MCP·도구 연결", score: 59 }, { category: "MCP·도구 연결", score: 56 },
  ];
  const { blips, emerging } = layout(cards, 3);
  const radius = blips.map((p) => Math.hypot(p.x - 150, p.y - 150));
  assert.ok(radius[0] < 50 && radius[1] < 50);
  assert.ok(radius[2] > 100 && radius[3] > 100);
  for (let i = 0; i < blips.length; i++) {
    for (let j = i + 1; j < blips.length; j++) {
      assert.ok(Math.hypot(blips[i].x - blips[j].x, blips[i].y - blips[j].y) >= 21.9, `blips ${i},${j} overlap`);
    }
  }
  assert.equal(emerging.length, 3);
  for (const p of [...blips, ...emerging]) assert.ok(p.y <= 150 - 13 + 1e-9, `point ${p.y} touches the baseline`);
});
