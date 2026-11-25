import { useEffect, useState } from "react";
import { ApiError, getMe, type Me } from "./api";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { HistoryPage } from "./HistoryPage";
import { LedgerPage } from "./LedgerPage";
import { QueuePanel } from "./QueuePanel";
import { RunForm } from "./RunForm";
import { RunPage } from "./RunPage";

// Hash routes keep the static bundle servable from any path: "#/" and "#/runs/<id>".
function useRoute(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash.replace(/^#/, "") || "/";
}

type Session = { state: "loading" } | { state: "out" } | { state: "refused"; message: string }
  | { state: "in"; me: Me };

export function App() {
  const [session, setSession] = useState<Session>({ state: "loading" });
  const route = useRoute();

  useEffect(() => {
    getMe()
      .then((me) => setSession({ state: "in", me }))
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) setSession({ state: "out" });
        else setSession({ state: "refused", message: e instanceof Error ? e.message : String(e) });
      });
  }, []);

  if (session.state === "loading") return <main><p>Loading…</p></main>;
  if (session.state === "out") {
    return (
      <main>
        <h1>PagerDuty Ops</h1>
        <p><a className="button" href="/auth/login">Sign in with Okta</a></p>
      </main>
    );
  }
  if (session.state === "refused") {
    return <main><h1>PagerDuty Ops</h1><p className="error" role="alert">{session.message}</p></main>;
  }

  const runMatch = route.match(/^\/runs\/([^/]+)$/);
  return (
    <>
      <header>
        <a href="#/" className="brand">PagerDuty Ops</a>
        <nav><a href="#/">Run</a> · <a href="#/history">History</a> · <a href="#/ledger">Ledger</a></nav>
        <span>{session.me.name} · {session.me.role} · <a href="/auth/logout">Sign out</a></span>
      </header>
      <main>{runMatch ? <RunPage id={decodeURIComponent(runMatch[1])} me={session.me} />
        : route.startsWith("/history") ? <HistoryPage hash={route} />
        : route.startsWith("/ledger") ? <LedgerPage hash={route} />
        : <><RunForm role={session.me.role} /><QueuePanel /><ApprovalsPanel /></>}</main>
    </>
  );
}
