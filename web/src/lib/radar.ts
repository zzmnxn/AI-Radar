// Half-circle radar geometry. Center = must-know, outer = watch.

export const RING_LABELS = ["꼭 알기", "알아두기", "관찰"] as const;
export type Ring = 0 | 1 | 2;

/** Score thresholds (0-100) for the inner two rings. */
export const MUST_KNOW = 85;
export const GOOD_TO_KNOW = 70;

export function ringOf(score: number): Ring {
  return score >= MUST_KNOW ? 0 : score >= GOOD_TO_KNOW ? 1 : 2;
}

/** Interest categories in config/interests.yaml order; each gets a fixed slice of the half circle. */
export const CATEGORIES = [
  "AI 코딩 도구", "에이전트 확장", "MCP·도구 연결", "에이전트 설계·스택", "컨텍스트·메모리",
  "GenUI·AI 인터페이스", "AI 개발 워크플로", "새 모델·API", "로컬·오픈 모델", "새 AI 서비스",
  "에이전트 작업 환경", "AI 활용 패턴",
];

export const SIZE = { width: 300, cx: 150, cy: 150, bands: [50, 100, 145] } as const;
const RADIUS = [32, 76, 118] as const; // where blips of each ring sit
const EMERGING_RADIUS = 138;
const MIN_ANGLE = Math.PI * 0.07;
const MAX_ANGLE = Math.PI * 0.93;
const GAP = 22; // minimum distance between blip centers, in viewBox units
const CLEARANCE = 13; // blip radius + margin above the baseline

export type Point = { x: number; y: number };

/** Preferred angle for a category, measured from the right (0) to the left (π). */
function categoryAngle(category: string) {
  const index = CATEGORIES.indexOf(category);
  const slot = index < 0 ? CATEGORIES.length : index;
  const slots = CATEGORIES.length + 1;
  return Math.PI - ((slot + 0.5) / slots) * Math.PI;
}

/** Push angles apart so neighbours on the same ring keep `gap` distance, staying inside the arc. */
export function spread(angles: number[], radius: number, gap = GAP): number[] {
  const step = gap / radius;
  // Keep a blip's circle clear of the baseline: radius * sin(angle) >= CLEARANCE.
  const lo = Math.max(MIN_ANGLE, Math.asin(Math.min(1, CLEARANCE / radius)));
  const hi = Math.PI - lo;
  const order = angles.map((angle, i) => [angle, i] as const).sort((a, b) => a[0] - b[0]);
  const out = order.map(([angle]) => Math.min(Math.max(angle, lo), hi));
  for (let i = 1; i < out.length; i++) out[i] = Math.max(out[i], out[i - 1] + step);
  if (out.length) out[out.length - 1] = Math.min(out[out.length - 1], hi);
  for (let i = out.length - 2; i >= 0; i--) out[i] = Math.min(out[i], out[i + 1] - step);
  if (out.length && out[0] < lo) {
    const span = (hi - lo) / out.length;
    out.forEach((_, i) => { out[i] = lo + span * (i + 0.5); });
  }
  const result = new Array<number>(angles.length);
  order.forEach(([, index], i) => { result[index] = out[i]; });
  return result;
}

function point(angle: number, radius: number): Point {
  return { x: SIZE.cx + radius * Math.cos(angle), y: SIZE.cy - radius * Math.sin(angle) };
}

/** Blip positions for cards (same order as input) and evenly spaced emerging markers. */
export function layout(cards: { category: string; score: number }[], emergingCount: number) {
  const blips = new Array<Point>(cards.length);
  for (const ring of [0, 1, 2] as Ring[]) {
    const members = cards.map((card, i) => [card, i] as const).filter(([card]) => ringOf(card.score) === ring);
    const angles = spread(members.map(([card]) => categoryAngle(card.category)), RADIUS[ring]);
    members.forEach(([, index], i) => { blips[index] = point(angles[i], RADIUS[ring]); });
  }
  const emerging = Array.from({ length: emergingCount }, (_, i) =>
    point(MAX_ANGLE - ((i + 0.5) / emergingCount) * (MAX_ANGLE - MIN_ANGLE), EMERGING_RADIUS));
  return { blips, emerging };
}

/** Briefing order: ring first, then score; equals score descending. Stable for ties. */
export function byImportance<T extends { score: number }>(cards: T[]): T[] {
  return [...cards].sort((a, b) => b.score - a.score);
}
