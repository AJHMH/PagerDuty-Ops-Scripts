import { useEffect, useState } from "react";
import { ApiError, getMe, type Me } from "./api";
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
        <span>{session.me.name} · {session.me.role} · <a href="/auth/logout">Sign out</a></span>
      </header>
      <main>{runMatch ? <RunPage id={decodeURIComponent(runMatch[1])} /> : <RunForm role={session.me.role} />}</main>
    </>
  );
}
