import { motion } from "motion/react";
import { useEffect, useState, type ComponentType } from "react";
import { Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";
import { api, invalidate, useApi, type Meta, type WsContext } from "./api";
import { EASE, ErrorNote, PageSkeleton, Spinner, ToastProvider } from "./components/ui";
import { FixtureBand, MobileBar, PAGES, RunHead, SETUP, ShellCtx, Sidebar } from "./shell";
import { useReducedMotion } from "./util";
import { LiveProvider } from "./live";
import Overview from "./pages/Overview";
import Market from "./pages/Market";
import Metrics from "./pages/Metrics";
import Voice from "./pages/Voice";
import Board from "./pages/Board";
import Ask from "./pages/Ask";
import Briefs from "./pages/Briefs";
import Actions from "./pages/Actions";
import Inspector from "./pages/Inspector";
import Evals from "./pages/Evals";
import Workspaces from "./pages/Workspaces";

const COMPONENTS: Record<string, ComponentType> = {
  overview: Overview,
  market: Market,
  metrics: Metrics,
  voice: Voice,
  board: Board,
  ask: Ask,
  briefs: Briefs,
  actions: Actions,
  inspector: Inspector,
  evals: Evals,
  workspaces: Workspaces,
};

function Login({ onDone }: { onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <main className="login">
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          try {
            await api("/login", { method: "POST", json: { password: pw } });
            onDone();
          } catch (ex) {
            setErr((ex as Error).message);
          } finally {
            setBusy(false);
          }
        }}
      >
        <h1 className="page-title" style={{ fontSize: "var(--fs-24)" }}>
          Sign in to FieldNote
        </h1>
        <p className="hint">This dashboard is password protected (FIELDNOTE_DASHBOARD_PASSWORD).</p>
        <label className="field">
          <span>Password</span>
          <input className="input" type="password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)} aria-invalid={!!err} />
        </label>
        {err ? <ErrorNote>{err}</ErrorNote> : null}
        <button className="btn primary" disabled={busy || !pw}>
          {busy ? <Spinner /> : null} Sign in
        </button>
      </form>
    </main>
  );
}

function WorkspaceView({ meta, reloadMeta }: { meta: Meta; reloadMeta: () => void }) {
  const { ws: wsParam = "_", page = "overview" } = useParams();
  const loc = useLocation();
  const reduced = useReducedMotion();
  const [menu, setMenu] = useState(false);
  const valid = (meta.workspaces ?? []).filter((w) => w.valid).map((w) => w.name);
  const ws = valid.includes(wsParam) ? wsParam : null;
  const ctxApi = useApi<WsContext>(ws ? `/ws/${ws}/context` : null);
  const ctx = ws ? (ctxApi.data ?? null) : null;

  useEffect(() => setMenu(false), [loc.pathname]);

  if (wsParam !== "_" && !ws) {
    const fallback = valid[0];
    return <Navigate to={fallback ? `/${fallback}/${page}` : "/_/workspaces"} replace />;
  }
  if (!ws && page !== "workspaces") return <Navigate to="/_/workspaces" replace />;

  const Page = COMPONENTS[page];
  if (!Page) return <Navigate to={`/${wsParam}/overview`} replace />;
  const title = page === "workspaces" ? SETUP.title : (PAGES.find((p) => p.path === page)?.title ?? "");

  return (
    <ShellCtx.Provider
      value={{
        meta,
        ws,
        ctx,
        reloadMeta,
        reloadCtx: () => {
          if (ws) invalidate(`/ws/${ws}/`);
          void ctxApi.reload();
        },
      }}
    >
      <LiveProvider ws={ws}>
      <div className="app">
        <Sidebar open={menu} onNavigate={() => setMenu(false)} />
        {menu ? <div className="scrim-overlay" onClick={() => setMenu(false)} aria-hidden /> : null}
        <div className="main">
          <MobileBar onMenu={() => setMenu(true)} />
          {ctx?.fixture ? <FixtureBand /> : null}
          <RunHead page={title} />
          <main className="page" id="content">
            {ws && !ctx ? (
              ctxApi.error ? (
                <ErrorNote>{ctxApi.error}</ErrorNote>
              ) : (
                <PageSkeleton />
              )
            ) : (
              <motion.div
                key={`${ws}-${page}`}
                initial={reduced ? false : { y: 10, filter: "blur(2px)" }}
                animate={{ y: 0, filter: "blur(0px)" }}
                transition={{ duration: 0.28, ease: EASE }}
              >
                <Page />
              </motion.div>
            )}
          </main>
        </div>
      </div>
      </LiveProvider>
    </ShellCtx.Provider>
  );
}

function Home({ meta }: { meta: Meta }) {
  const valid = (meta.workspaces ?? []).filter((w) => w.valid).map((w) => w.name);
  const target = valid.includes(meta.default_workspace ?? "") ? meta.default_workspace : valid[0];
  return <Navigate to={target ? `/${target}/overview` : "/_/workspaces"} replace />;
}

export default function App() {
  const meta = useApi<Meta>("/meta");
  useEffect(() => {
    const on = () => void meta.reload();
    window.addEventListener("fn:unauthorized", on);
    return () => window.removeEventListener("fn:unauthorized", on);
  }, [meta]);

  if (!meta.data) {
    return meta.error ? (
      <main className="login">
        <ErrorNote>Could not reach the FieldNote server: {meta.error}</ErrorNote>
      </main>
    ) : (
      <main className="page" style={{ maxWidth: 900, margin: "0 auto" }}>
        <PageSkeleton />
      </main>
    );
  }
  if (meta.data.auth_required && !meta.data.authed) return <Login onDone={() => void meta.reload()} />;

  return (
    <ToastProvider>
      <Routes>
        <Route path="/" element={<Home meta={meta.data} />} />
        <Route path="/:ws" element={<Home meta={meta.data} />} />
        <Route path="/:ws/:page" element={<WorkspaceView meta={meta.data} reloadMeta={() => void meta.reload()} />} />
        <Route path="*" element={<Home meta={meta.data} />} />
      </Routes>
    </ToastProvider>
  );
}
