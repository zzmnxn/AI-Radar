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

function Story({ card, featured = false, index = 0 }: { card: Card; featured?: boolean; index?: number }) {
  const Heading = featured ? "h2" : "h3";
  return (
    <article className={featured ? "story featured" : "story followup"} id={card.card_id}>
      <div className="story-label">
        <span className="story-no">{String(index + 1).padStart(2, "0")}</span>
        {featured && <span>Featured</span>}
        <span>{card.category}</span>
        {card.update_of && <span className="update">업데이트</span>}
      </div>
      <Heading>{card.title_ko}</Heading>
      <div className="metadata">
        {card.sources.map((source) => <span key={source.url}>{source.org}</span>)}
        {card.published_at && <time dateTime={card.published_at}>{dateTime.format(new Date(card.published_at))} KST</time>}
      </div>
      <div className="badges">
        <span className="badge"><b>{labels.availability}</b> {availability[card.availability]}</span>
        <span className="badge access"><b>{labels.access}</b> {access[card.access.type]}</span>
      </div>
      <dl className="sections">
        {(["what", "why", "how"] as const).map((role) => (
          <div key={role}>
            <dt>{labels[role]}</dt>
            <dd>{card[role].length ? card[role].map((sentence, index) => (
              <p key={index}>{sentence}</p>
            )) : <p className="muted">사용법 근거 부족. 원문을 참고하세요.</p>}</dd>
          </div>
        ))}
      </dl>
      {!!card.tags.length && <p className="tags">{card.tags.map((tag) => `#${tag}`).join(" · ")}</p>}
      <div className="sources">
        {card.sources.map((source) => (
          <a key={source.url} href={safeLink(source.url)} target="_blank" rel="noreferrer">
            {card.sources.length > 1 ? `${source.org} 원문` : "원문 보기"} <span aria-hidden="true">↗</span>
          </a>
        ))}
      </div>
      <details>
        <summary>인용 근거 {card.verification_summary.claims.length}개</summary>
        {card.verification_summary.claims.map((claim, index) => (
          <figure key={index}>
            <figcaption><a href={safeLink(claim.evidence_url)}>{labels[claim.role]}</a></figcaption>
            <blockquote>{claim.evidence_quote}</blockquote>
          </figure>
        ))}
        {card.verification_summary.uncertainty && <p>{card.verification_summary.uncertainty}</p>}
      </details>
    </article>
  );
}

export function Edition({ briefing, success }: { briefing: Briefing; success?: string }) {
  const [featured, ...followups] = briefing.cards;
  const counts = briefing.counts;
  return (
    <>
      <section className="lead">
        <div className="edition-line">
          <span className="issue">Issue No.{issueNumber(briefing.date)}</span>
          <time dateTime={briefing.date}>{briefing.date.replaceAll("-", ".")}</time>
          {success && <span>마지막 성공 {success}</span>}
        </div>
        <p className="lead-kicker">오늘의 한 줄 브리핑</p>
        <h1>{briefing.headline_ko}</h1>
        <p className="lead-note">
          원본 {counts.raw.toLocaleString("ko-KR")}건 중 {counts.analyzed.toLocaleString("ko-KR")}건을 평가해
          {" "}{counts.selected}건을 골랐습니다.
        </p>
      </section>
      {featured && <section className={followups.length ? "stories" : "stories single"} aria-label="브리핑 기사">
        <Story card={featured} featured index={0} />
        {!!followups.length && <section className="followups">
          <h2>함께 볼 소식</h2>
          {followups.map((card, i) => <Story card={card} key={card.card_id} index={i + 1} />)}
        </section>}
      </section>}
      {!!briefing.emerging.length && <section className="emerging">
        <p className="section-kicker">Emerging Radar</p>
        <h2>새롭게 떠오르는 주제</h2>
        <ul>{briefing.emerging.map((item) => (
          <li key={item.tag}>
            <span>{item.kind === "discovery" ? "발견" : "관찰"}</span>
            <strong>{item.tag}</strong>
            <p>{item.reason_ko}</p>
          </li>
        ))}</ul>
      </section>}
    </>
  );
}
