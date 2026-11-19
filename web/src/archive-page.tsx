import { useCallback, useState } from "react";
import { Edition } from "./briefing";
import { archiveYears, filterArchive } from "./lib/archive";
import { loadArchive, loadBriefing, SchemaVersionError } from "./lib/data";
import { useResource } from "./lib/resource";
import type { Manifest, YearIndex } from "./lib/types.gen";

export function DataMessage({ resource }: { resource: { error: unknown } | null }) {
  return <p className="muted" role="status">{
    resource?.error instanceof SchemaVersionError
      ? "이 페이지에서 읽을 수 없는 데이터 버전입니다."
      : resource ? "데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."
      : "불러오는 중입니다."
  }</p>;
}

function ArchivedEdition({ manifest, date }: { manifest: Manifest; date: string }) {
  const read = useCallback((signal: AbortSignal) => loadBriefing(manifest, date, signal), [manifest, date]);
  const result = useResource(read);
  return (
    <>
      <a className="back-link" href="#archive">← 아카이브</a>
      {result && "data" in result ? <Edition briefing={result.data} />
        : <section className="lead"><h1>{date.replaceAll("-", ".")}</h1><DataMessage resource={result} /></section>}
    </>
  );
}

export function Archive({ manifest, date }: { manifest: Manifest; date: string | null }) {
  const years = archiveYears(manifest.published?.files ?? {});
  const initialYear = date?.slice(0, 4) ?? "";
  const [year, setYear] = useState(years.includes(initialYear) ? initialYear : years[0] ?? "");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [tag, setTag] = useState("");
  const read = useCallback(
    (signal: AbortSignal) => date || !year ? Promise.resolve<YearIndex>({}) : loadArchive(manifest, year, signal),
    [manifest, year, date],
  );
  const result = useResource(read);

  if (date) return <ArchivedEdition manifest={manifest} date={date} />;

  const index = result && "data" in result ? result.data : null;
  const entries = Object.values(index ?? {});
  const sorted = (values: string[]) => [...new Set(values)].sort((a, b) => a.localeCompare(b, "ko"));
  const categories = sorted(entries.flatMap((entry) => entry.categories));
  const tags = sorted(entries.flatMap((entry) => entry.tags));
  const rows = index ? filterArchive(index, query, category, tag) : [];

  return (
    <>
      <section className="lead archive-lead"><h1>아카이브</h1></section>
      {years.length > 0 ? <>
        <form className="archive-filters" role="search" onSubmit={(event) => event.preventDefault()}>
          <label className="search-field">제목·키워드
            <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="검색어 입력" />
          </label>
          <label>연도
            <select value={year} onChange={(event) => {
              setYear(event.target.value);
              setCategory("");
              setTag("");
            }}>
              {years.map((value) => <option key={value} value={value}>{value}년</option>)}
            </select>
          </label>
          <label>카테고리
            <select value={category} onChange={(event) => setCategory(event.target.value)}>
              <option value="">전체</option>
              {categories.map((value) => <option key={value}>{value}</option>)}
            </select>
          </label>
          <label>태그
            <select value={tag} onChange={(event) => setTag(event.target.value)}>
              <option value="">전체</option>
              {tags.map((value) => <option key={value}>{value}</option>)}
            </select>
          </label>
        </form>
        {!index && <DataMessage resource={result && "error" in result ? result : null} />}
        {index && <>
          <p className="result-count" role="status">{rows.length ? `브리핑 ${rows.length}건` : "검색 조건에 맞는 브리핑이 없습니다."}</p>
          <ol className="archive-list">
            {rows.map(([day, entry]) => (
              <li key={day}>
                <a className="archive-row" href={`#archive/${day}`}>
                  <time dateTime={day}>{day.replaceAll("-", ".")}</time>
                  <div>
                    <h2>{entry.headline_ko}</h2>
                    <p>{entry.categories.join(" · ")}{entry.categories.length ? " · " : ""}카드 {entry.card_count}건</p>
                  </div>
                  <span className="row-arrow" aria-hidden="true">↗</span>
                </a>
              </li>
            ))}
          </ol>
        </>}
      </> : <p className="muted">아카이브에 등록된 브리핑이 없습니다.</p>}
    </>
  );
}
