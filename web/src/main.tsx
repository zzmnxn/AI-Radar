import { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Archive, DataMessage } from "./archive-page";
import { Edition, dateTime } from "./briefing";
import { readRoute } from "./lib/archive";
import { loadBriefing, loadSnapshot, type Snapshot } from "./lib/data";
import { useResource } from "./lib/resource";
import { briefingStatus } from "./lib/status";
import "./style.css";

function Today({ manifest }: Snapshot) {
  const published = manifest.published;
  const read = useCallback(
    (signal: AbortSignal) => published ? loadBriefing(manifest, published.date, signal) : Promise.resolve(null),
    [manifest, published],
  );
  const result = useResource(read);
  const status = briefingStatus(manifest, new Date());
  const success = manifest.last_success_at
    ? dateTime.format(new Date(manifest.last_success_at)) + " KST"
    : "기록 없음";

  return (
    <>
      {status !== "ok" && <p className="notice" role="status">
        <strong>{status === "failed" ? "오늘 브리핑 생성 실패" : "오늘 브리핑 미발행"}</strong>
        <span>마지막 성공 {success}</span>
      </p>}
      {result && "data" in result && result.data ? <Edition briefing={result.data} success={status === "ok" ? success : undefined} />
        : <section className="lead"><h1>오늘의 브리핑</h1>
          {!published ? <p>첫 브리핑을 기다리고 있습니다.</p>
            : <DataMessage resource={result && "error" in result ? result : null} />}
        </section>}
    </>
  );
}

function Footer({ manifest, build }: Snapshot) {
  const sources = manifest.last_run.sources;
  return (
    <footer className="wrap">
      <span>수집 완료 {sources.complete} · 부분 {sources.partial} · 실패 {sources.failed}</span>
      {build && <span>빌드 {build.commit.slice(0, 7)} · {dateTime.format(new Date(build.built_at))} KST</span>}
    </footer>
  );
}

function App() {
  const [route, setRoute] = useState(() => readRoute(window.location.hash));
  const snapshot = useResource(loadSnapshot);
  const heading = route.page === "today" ? "오늘의 브리핑" : "아카이브";

  useEffect(() => {
    const change = () => {
      setRoute(readRoute(window.location.hash));
      document.getElementById("content")?.focus();
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);

  const data = snapshot && "data" in snapshot ? snapshot.data : null;
  return (
    <>
      <header className="masthead">
        <div className="wrap masthead-inner">
          <a className="wordmark" href="#today" aria-label="AI Radar 오늘의 브리핑">AI <span>Radar</span></a>
          <nav aria-label="주 메뉴">
            <a href="#today" aria-current={route.page === "today" ? "page" : undefined}>오늘</a>
            <a href="#archive" aria-current={route.page === "archive" ? "page" : undefined}>아카이브</a>
          </nav>
        </div>
      </header>
      <main className="wrap" id="content" tabIndex={-1}>
        {data ? (route.page === "today" ? <Today {...data} /> : <Archive manifest={data.manifest} date={route.date} />)
          : <section className="lead">
            <h1>{heading}</h1>
            {snapshot && "data" in snapshot
              ? <p>첫 브리핑을 기다리고 있습니다.</p>
              : <DataMessage resource={snapshot} />}
          </section>}
      </main>
      {data && <Footer {...data} />}
    </>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
