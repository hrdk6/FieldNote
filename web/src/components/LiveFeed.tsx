/* The wire: newest items collected from real sources, newest first. Items that arrive while the page is open
   slide in at the top and carry a brief "New" mark; everything else stays still. */

import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { FileDiff, MessageSquare, Newspaper, PlayCircle, Rss } from "lucide-react";
import { useApi, type LatestItem } from "../api";
import { ago, useNow } from "../live";
import { entityColor } from "../util";
import { EASE, Empty, Mark, Seg, Sk } from "./ui";

const SEEN = new Map<string, Set<string>>();

function Glyph({ item }: { item: LatestItem }) {
  const p = { size: 15, strokeWidth: 1.8, "aria-hidden": true } as const;
  if (item.type === "change") return <FileDiff {...p} />;
  if (item.source_type === "reddit") return <MessageSquare {...p} />;
  if (item.source_type === "youtube") return <PlayCircle {...p} />;
  if (item.source_type === "news") return <Newspaper {...p} />;
  return <Rss {...p} />;
}

const TYPE_LABEL: Record<string, string> = { news: "News", reddit: "Reddit", youtube: "YouTube", page: "Page change", other: "Source" };

export function LiveFeed({
  ws,
  entities,
  limit = 8,
  onCount,
}: {
  ws: string;
  entities: string[];
  limit?: number;
  onCount?: (n: number) => void;
}) {
  const [scope, setScope] = useState<"tracked" | "all">("tracked");
  const [more, setMore] = useState(false);
  const n = more ? limit * 4 : limit;
  const { data } = useApi<{ items: LatestItem[]; tagged_in_window: number }>(
    `/ws/${ws}/latest?limit=${n}&tracked=${scope === "tracked" ? 1 : 0}`,
  );
  const now = useNow(30000);
  const seenKey = `${ws}:${scope}`;
  const first = useRef(!SEEN.has(seenKey));
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  // Mark items that arrived after the first render of this feed as new.
  useEffect(() => {
    if (!data) return;
    const seen = SEEN.get(seenKey) ?? new Set<string>();
    if (first.current) {
      data.items.forEach((i) => seen.add(i.key));
      first.current = false;
    } else {
      const added = data.items.filter((i) => !seen.has(i.key)).map((i) => i.key);
      if (added.length) {
        setFresh(new Set(added));
        added.forEach((k) => seen.add(k));
        const t = window.setTimeout(() => setFresh(new Set()), 12000);
        SEEN.set(seenKey, seen);
        return () => window.clearTimeout(t);
      }
    }
    SEEN.set(seenKey, seen);
  }, [data, seenKey]);

  useEffect(() => {
    if (data) onCount?.(data.items.length);
  }, [data, onCount]);

  // With no tagged items at all, "Tracked companies" would be empty: start on everything instead.
  useEffect(() => {
    if (data && scope === "tracked" && data.items.length === 0 && !first.current) {
      first.current = !SEEN.has(`${ws}:all`);
      setScope("all");
    }
  }, [data, scope, ws]);

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 10 }}>
        <Seg
          label="Show"
          value={scope}
          options={["tracked", "all"] as const}
          format={(v) => (v === "tracked" ? "Tracked companies" : "Everything collected")}
          onChange={(v) => {
            first.current = !SEEN.has(`${ws}:${v}`);
            setScope(v);
          }}
        />
      </div>
      {!data ? (
        <div>
          {[0, 1, 2, 3].map((i) => (
            <div key={i} style={{ padding: "12px 0", borderBottom: "1px solid var(--rule-soft)" }}>
              <Sk w="70%" />
              <Sk w="30%" h={10} style={{ marginTop: 8 }} />
            </div>
          ))}
        </div>
      ) : data.items.length ? (
        <ol className="wire">
          <AnimatePresence initial={false}>
            {data.items.map((it) => {
              const isNew = fresh.has(it.key);
              return (
                <motion.li
                  key={it.key}
                  layout="position"
                  className={isNew ? "is-new" : undefined}
                  initial={{ opacity: 0, y: -8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.32, ease: EASE }}
                >
                  <span className="wire-time" title={it.at ? `${it.at} UTC` : undefined}>
                    {ago(it.at, now)}
                  </span>
                  <span className="wire-glyph" title={TYPE_LABEL[it.source_type] ?? it.source_type}>
                    <Glyph item={it} />
                  </span>
                  <span className="wire-body">
                    <a href={it.url} target="_blank" rel="noreferrer" className="wire-title">
                      {it.title}
                    </a>
                    <span className="wire-meta">
                      {isNew ? <Mark kind="plain">New</Mark> : null}
                      <span>{it.type === "change" ? `${it.kind ?? "page"} change` : it.source}</span>
                      {it.entities.map((e) => (
                        <span key={e} className="entity" style={{ fontWeight: 560, fontSize: "inherit" }}>
                          <i className="swatch" style={{ background: entityColor(entities, e) }} />
                          {e}
                        </span>
                      ))}
                    </span>
                  </span>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ol>
      ) : (
        <Empty
          title={scope === "tracked" ? "No collected item names a tracked company yet." : "Nothing collected yet."}
          hint={
            scope === "tracked"
              ? "Switch to Everything collected to see the whole feed; items are tagged as they arrive."
              : "The first live collection fills this feed; it refreshes on its own."
          }
        />
      )}
      {data && data.items.length >= n ? (
        <button type="button" className="disclose" onClick={() => setMore((m) => !m)} style={{ marginTop: 12 }}>
          {more ? "Show fewer" : `Show ${limit * 3} more`}
        </button>
      ) : null}
    </div>
  );
}
