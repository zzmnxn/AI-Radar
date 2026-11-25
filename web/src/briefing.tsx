import { useState } from "react";
import { byImportance, layout, RING_LABELS, ringOf, SIZE, type Ring } from "./lib/radar";
import { splitCode } from "./lib/text";
import type { Access, Briefing, Card } from "./lib/types.gen";

const availability: Record<Card["availability"], string> = {
  available: "이용 가능", beta: "베타", preview: "프리뷰", research: "연구",
  announced: "발표", unknown: "미확인",
};
const access: Record<Access["type"], string> = {
  open: "공개", signup: "가입 필요", waitlist: "대기 명단", paid: "유료",
  enterprise: "기업용", region_limited: "지역 제한", api_only: "API 전용", unknown: "미확인",
};
const labels = {
  what: "무엇이 바뀌었나", why: "왜 중요한가", how: "어떻게 쓰나",
  availability: "이용 단계", access: "접근 조건",
};

export const dateTime = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", year: "numeric", month: "short", day: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

const FIRST_ISSUE = Date.UTC(2026, 10, 18);
export function issueNumber(date: string) {
  const days = Math.round((Date.parse(`${date}T00:00:00Z`) - FIRST_ISSUE) / 86_400_000);
  return Math.max(days + 1, 1);
}

function safeLink(url: string) {
  return /^https?:\/\//i.test(url) ? url : undefined;
}

function Text({ sentence }: { sentence: string }) {
  return <>{splitCode(sentence).map((part, i) => part.code ? <code key={i}>{part.text}</code> : part.text)}</>;
}

function Sentences({ items }: { items: string[] }) {
  return <>{items.map((sentence, i) => <p key={i}><Text sentence={sentence} /></p>)}</>;
}

const no = (index: number) => String(index + 1);

function Radar({ cards, emerging, active, onPick }: {
  cards: Card[]; emerging: Briefing["emerging"]; active: string | null; onPick: (id: string) => void;
}) {
  const { blips, emerging: marks } = layout(cards, emerging.length);
  const { width, cx, cy, bands } = SIZE;
  return (
    <svg className="radar" viewBox={`0 0 ${width} ${width}`} aria-hidden="true">
      {[...bands].reverse().map((r, i) => (
        <circle key={r} className={`band band-${2 - i}`} cx={cx} cy={cy} r={r} />
      ))}
      <line className="axis" x1={cx - bands[2]} y1={cy} x2={cx + bands[2]} y2={cy} />
      <line className="axis" x1={cx} y1={cy - bands[2]} x2={cx} y2={cy + bands[2]} />
      {RING_LABELS.map((label, ring) => (
        <text key={label} className="ring-label" x={cx} y={cy + (ring ? (bands[ring - 1] + bands[ring]) / 2 : bands[0] / 2)} textAnchor="middle">{label}</text>
      ))}
      {marks.map((p, i) => (
        <path key={emerging[i].tag} className={`mark ${emerging[i].kind}`} d={`M ${p.x} ${p.y - 6} l 6 10 h -12 z`}>
          <title>{emerging[i].tag}</title>
        </path>
      ))}
      {cards.map((card, i) => (
        <g key={card.card_id} className={`blip ring-${ringOf(card.score)}${active === card.card_id ? " active" : ""}`}
          onClick={() => onPick(card.card_id)}>
          <circle cx={blips[i].x} cy={blips[i].y} r={active === card.card_id ? 11 : 9} />
          <text x={blips[i].x} y={blips[i].y + 3.5} textAnchor="middle">{no(i)}</text>
        </g>
      ))}
    </svg>
  );
}

function Item({ card, index, open, onToggle, onHover }: {
  card: Card; index: number; open: boolean; onToggle: (open: boolean) => void; onHover: (on: boolean) => void;
}) {
  const ring: Ring = ringOf(card.score);
  const claims = card.verification_summary.claims;
  const [why, ...moreWhy] = card.why;
  return (
    <li className={`item ring-${ring}`} id={card.card_id}
      onMouseEnter={() => onHover(true)} onMouseLeave={() => onHover(false)}>
      <details open={open} onToggle={(event) => onToggle(event.currentTarget.open)}>
        <summary>
          <span className="item-no">{String(index + 1).padStart(2, "0")}</span>
          <span className="item-head">
            <span className="item-category">{card.category}</span>
            <span className="item-title">{card.title_ko}{card.update_of && <span className="update"> 업데이트</span>}</span>
            {card.what[0] && <span className="item-what"><Text sentence={card.what[0]} /></span>}

          </span>
        </summary>
        <div className="item-body">
          <p className="metadata">
            <span>{card.category}</span>
            {card.sources.map((source) => <span key={source.url}>{source.org}</span>)}
            {card.published_at && <time dateTime={card.published_at}>{dateTime.format(new Date(card.published_at))}</time>}
            {card.availability !== "unknown" && <span className="badge">{availability[card.availability]}</span>}
            {card.access.type !== "unknown" && <span className="badge access">{access[card.access.type]}</span>}
          </p>
          {card.what.length > 1 && <Sentences items={card.what.slice(1)} />}
          {why && <div className="item-why-full">
            <b>{labels.why}</b><Sentences items={[why, ...moreWhy]} />
          </div>}
          <p className="sources">
            {card.sources.map((source) => (
              <a key={source.url} href={safeLink(source.url)} target="_blank" rel="noreferrer">
                {card.sources.length > 1 ? `${source.org} 원문` : "원문 보기"} <span aria-hidden="true">↗</span>
              </a>
            ))}
          </p>
          <details className="more">
            <summary>사용법 · 인용 근거 {claims.length}개</summary>
            <dl>
              <dt>{labels.how}</dt>
              <dd>{card.how.length ? <Sentences items={card.how} /> : <p className="muted">사용법 근거 부족. 원문을 참고하세요.</p>}</dd>
              {!!claims.length && <><dt>인용 근거</dt><dd>
                {claims.map((claim, i) => (
                  <blockquote key={i}>
                    <a href={safeLink(claim.evidence_url)}>{labels[claim.role]}</a> {claim.evidence_quote}
                  </blockquote>
                ))}
                {card.verification_summary.uncertainty && <p className="muted">{card.verification_summary.uncertainty}</p>}
              </dd></>}
              {!!card.tags.length && <><dt>태그</dt><dd className="tags">{card.tags.map((tag) => <span key={tag}>#{tag}</span>)}</dd></>}
            </dl>
          </details>
        </div>
      </details>
    </li>
  );
}

export function Edition({ briefing, success }: { briefing: Briefing; success?: string }) {
  const cards = byImportance(briefing.cards);
  const [opened, setOpened] = useState<Set<string>>(() => new Set());
  const [hovered, setHovered] = useState<string | null>(null);
  const [concept, setConcept] = useState<string | null>(null);
  const counts = briefing.counts;
  const toggle = (id: string, open: boolean) => setOpened((prev) => {
    if (prev.has(id) === open) return prev;
    const next = new Set(prev);
    if (open) next.add(id); else next.delete(id);
    return next;
  });
  const pick = (id: string) => {
    toggle(id, true);
    document.getElementById(id)?.scrollIntoView({ block: "nearest" });
  };
  const active = hovered ?? [...opened].at(-1) ?? null;
  const selectedConcept = briefing.emerging.find((item) => item.tag === concept);

  return (
    <section className="snapshot">
      <div className="edition-line">
        <time dateTime={briefing.date}>{briefing.date.replaceAll("-", ".")}</time>
        <span className="issue">No.{issueNumber(briefing.date)}</span>
        {success && <span>업데이트 {success}</span>}
      </div>
      <h1 className="headline">오늘의 AI 레이더</h1>
      {!!cards.length && <div className="radar-panel"><div className="radar-heading"><span>◉ SIGNAL RADAR</span><span>{cards.length} SIGNALS</span></div><Radar cards={cards} emerging={briefing.emerging} active={active} onPick={pick} /><p>중심에 가까울수록 중요한 소식 · 번호를 눌러 자세히 보기</p></div>}
      {!!cards.length && <div className="signals-heading"><div><span>DAILY SIGNALS</span><h2>오늘 포착된 소식</h2></div><span>{cards.length}건 · 중요도순</span></div>}
      {!!cards.length && <ol className="items" aria-label="오늘의 기사">
        {cards.map((card, i) => (
          <Item key={card.card_id} card={card} index={i} open={opened.has(card.card_id)}
            onToggle={(open) => toggle(card.card_id, open)}
            onHover={(on) => setHovered(on ? card.card_id : null)} />
        ))}
      </ol>}
      {!!briefing.emerging.length && <div className="concepts">
        <span className="concepts-label">▲ 떠오르는 개념</span>
        {briefing.emerging.map((item) => (
          <button type="button" key={item.tag} className={`chip ${item.kind}`} aria-expanded={concept === item.tag}
            onClick={() => setConcept(concept === item.tag ? null : item.tag)}>{item.tag}</button>
        ))}
        {selectedConcept && <div className="concept-details"><p className="concept-reason">{selectedConcept.reason_ko}</p>
          {selectedConcept.sources?.length ? <ul>{selectedConcept.sources.map((source) => {
            const related = cards.find((card) => card.event_id === source.event_id);
            return <li key={source.event_id}><a href={safeLink(source.url)} target="_blank" rel="noreferrer">{source.title} ↗</a><span> {source.org}{source.published_at ? ` · ${dateTime.format(new Date(source.published_at))}` : ""}</span>{related && <button type="button" onClick={() => pick(related.card_id)}>브리핑 보기</button>}</li>;
          })}</ul> : <p className="muted">이 브리핑에는 관련 자료가 저장되어 있지 않습니다.</p>}
        </div>}
      </div>}
      <p className="lead-note">원본 {counts.raw.toLocaleString("ko-KR")}건 중 {counts.analyzed.toLocaleString("ko-KR")}건을 평가해 {counts.selected}건을 골랐습니다.</p>
    </section>
  );
}
