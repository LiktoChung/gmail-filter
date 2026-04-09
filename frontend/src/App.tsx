import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactECharts from "echarts-for-react";
import "./App.css";

type AuthStatus = { authenticated: boolean };

type MsgRow = {
  id: string;
  subject?: string;
  from?: string;
  internalDate?: number;
  starred?: boolean;
  important?: boolean;
  /** Gmail SENT system label */
  sent?: boolean;
  /** Gmail INBOX (mail in inbox; archived mail may be false) */
  received?: boolean;
  /** Gmail UNREAD label */
  unread?: boolean;
};

type ListResp = {
  messages?: MsgRow[];
  nextPageToken?: string;
};

type PreviewResp = {
  preview: {
    html_document: string;
    subject: string;
    from: string;
    to: string;
    date: string;
  };
};

type DeletedRecent = { id: string; subject: string };

type JobPub = {
  id: string;
  kind: string;
  status: string;
  phase: string;
  processed: number;
  total_hint: number | null;
  percent: number | null;
  message: string;
  error: string | null;
  requests_per_minute?: number | null;
  adaptive_pause_seconds?: number | null;
  adaptive_workers?: number | null;
  deleted_recent?: DeletedRecent[] | null;
  queue_remaining?: number | null;
};

type AggResp = {
  group_by: string;
  items: { key: string; count: number }[];
  cached_total: number;
};

type CacheListResp = {
  group_by: string;
  key: string;
  total: number;
  messages: MsgRow[];
  next_offset: number | null;
};

type CacheListState = {
  group_by: string;
  key: string;
  total: number;
  nextOffset: number | null;
};

/** Mirrors backend `Settings` + `SyncBody` Gmail sync tuning. */
type GmailSyncRateSettings = {
  gmail_list_page_size: number;
  gmail_parallel_workers: number;
  gmail_enrich_chunk_size: number;
  gmail_sync_chunk_pause_seconds: number;
  gmail_list_page_pause_seconds: number;
  gmail_adaptive_sync: boolean;
};

const DEFAULT_GMAIL_SYNC_RATE: GmailSyncRateSettings = {
  gmail_list_page_size: 500,
  gmail_parallel_workers: 4,
  gmail_enrich_chunk_size: 8,
  gmail_sync_chunk_pause_seconds: 0.75,
  gmail_list_page_pause_seconds: 0.35,
  gmail_adaptive_sync: true,
};

/** Rough steady-state requests/min from list + metadata chunk timing (actual network varies). */
function estimatedGmailSyncRequestsPerMinute(sr: GmailSyncRateSettings): number {
  const L = Math.min(500, Math.max(1, sr.gmail_list_page_size));
  const C = Math.min(200, Math.max(1, sr.gmail_enrich_chunk_size));
  const pc = Math.max(0, sr.gmail_sync_chunk_pause_seconds);
  const pl = Math.max(0, sr.gmail_list_page_pause_seconds);
  const net = 0.35;
  const chunksPerPage = Math.ceil(L / C);
  const wallListPage = pl + chunksPerPage * (pc + net);
  const reqsPerPage = 1 + L;
  if (wallListPage <= 0) return 0;
  return Math.round((reqsPerPage / wallListPage) * 60);
}

/** Per-request page size for Gmail list + enrich (API max 500; lower = faster first paint). */
const GMAIL_LIST_LIMIT = 100;

function formatMsgDate(ms: number | undefined): string {
  if (ms == null || !ms) return "—";
  try {
    return new Date(ms).toLocaleString(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return "—";
  }
}

function dirHint(m: MsgRow): string {
  if (m.sent) return "Sent (SENT label)";
  if (m.received) return "Received (Inbox)";
  return "Other (e.g. archived, no Inbox)";
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...(init?.headers || {}),
  };
  const r = await fetch(path, { ...init, headers });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(t || r.statusText);
  }
  if (r.status === 204) return undefined as T;
  return r.json() as Promise<T>;
}

const SEARCH_IN = [
  "anywhere",
  "inbox",
  "trash",
  "spam",
  "sent",
  "drafts",
  "important",
  "starred",
  "snoozed",
];

const CATEGORIES = [
  "",
  "primary",
  "social",
  "promotions",
  "updates",
  "forums",
  "reservations",
  "purchases",
];

function readStoredTheme(): "light" | "dark" {
  try {
    return localStorage.getItem("theme") === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [rawMode, setRawMode] = useState(false);
  const [rawText, setRawText] = useState("");
  const [structured, setStructured] = useState<Record<string, string | boolean>>({
    search_in: "anywhere",
    has_attachment: false,
    exclude_chats: true,
  });
  const [readFilter, setReadFilter] = useState<"any" | "read" | "unread">("any");
  const [compiledQ, setCompiledQ] = useState("");
  const [messages, setMessages] = useState<MsgRow[]>([]);
  const [nextPage, setNextPage] = useState<string | undefined>();
  const [cacheList, setCacheList] = useState<CacheListState | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [previewDoc, setPreviewDoc] = useState<string | null>(null);
  const [previewMeta, setPreviewMeta] = useState<{
    subject: string;
    from: string;
    to: string;
    date: string;
  } | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<JobPub | null>(null);
  const [aggGroup, setAggGroup] = useState<"domain" | "sender" | "age" | "newsletter">("domain");
  const [agg, setAgg] = useState<AggResp | null>(null);
  const [chartFullscreen, setChartFullscreen] = useState(false);
  const [aggViewMode, setAggViewMode] = useState<"chart" | "list">("chart");
  const [theme, setTheme] = useState<"light" | "dark">(readStoredTheme);
  const [syncRate, setSyncRate] = useState<GmailSyncRateSettings>(DEFAULT_GMAIL_SYNC_RATE);
  const estimatedSyncRpm = useMemo(
    () => estimatedGmailSyncRequestsPerMinute(syncRate),
    [syncRate],
  );
  const chartShellRef = useRef<HTMLDivElement>(null);
  const chartWrapRef = useRef<HTMLDivElement>(null);
  const echartsInstRef = useRef<{ resize: () => void } | null>(null);
  const bulkPendingRef = useRef<{
    jobId: string;
    action: "archive" | "trash" | "read" | "unread";
    ids: string[];
  } | null>(null);

  const refreshAuth = useCallback(() => {
    api<AuthStatus>("/api/auth/status")
      .then(setAuth)
      .catch(() => setAuth({ authenticated: false }));
  }, []);

  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);

  useEffect(() => {
    api<GmailSyncRateSettings>("/api/settings/gmail-sync")
      .then(setSyncRate)
      .catch(() => {
        /* keep DEFAULT_GMAIL_SYNC_RATE */
      });
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("theme", theme);
    } catch {
      /* ignore */
    }
  }, [theme]);

  const compileQ = useCallback(async () => {
    setErr(null);
    const body: { structured: Record<string, unknown>; q: string | null } = {
      structured: {},
      q: null,
    };
    if (rawMode) {
      body.structured = { raw: rawText };
    } else {
      const s: Record<string, unknown> = { ...structured };
      if (readFilter === "read") {
        s.is_read = true;
        delete s.is_unread;
      } else if (readFilter === "unread") {
        s.is_unread = true;
        delete s.is_read;
      }
      body.structured = s;
    }
    const r = await api<{ q: string }>("/api/search/compile", {
      method: "POST",
      body: JSON.stringify(body),
    });
    setCompiledQ(r.q);
    return r.q;
  }, [rawMode, rawText, structured, readFilter]);

  const fetchMessageList = useCallback(async (q: string) => {
    setCacheList(null);
    const params = new URLSearchParams({
      q,
      limit: String(GMAIL_LIST_LIMIT),
    });
    const data = await api<ListResp>("/api/messages?" + params.toString());
    setMessages(data.messages || []);
    setNextPage(data.nextPageToken);
    setSelected(new Set());
  }, []);

  const runSearch = async () => {
    setLoading(true);
    setErr(null);
    try {
      const q = await compileQ();
      await fetchMessageList(q);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  const loadMore = async () => {
    if (cacheList?.nextOffset != null) {
      setLoading(true);
      setErr(null);
      try {
        const params = new URLSearchParams({
          group_by: cacheList.group_by,
          key: cacheList.key,
          limit: String(GMAIL_LIST_LIMIT),
          offset: String(cacheList.nextOffset),
        });
        const data = await api<CacheListResp>("/api/cache/messages?" + params.toString());
        setMessages((m) => [...m, ...(data.messages || [])]);
        setCacheList({
          group_by: data.group_by,
          key: data.key,
          total: data.total,
          nextOffset: data.next_offset,
        });
      } catch (e) {
        setErr(String(e));
      } finally {
        setLoading(false);
      }
      return;
    }
    if (!nextPage) return;
    setLoading(true);
    setErr(null);
    try {
      const params = new URLSearchParams({
        q: compiledQ,
        page_token: nextPage,
        limit: String(GMAIL_LIST_LIMIT),
      });
      const data = await api<ListResp>("/api/messages?" + params.toString());
      setMessages((m) => [...m, ...(data.messages || [])]);
      setNextPage(data.nextPageToken);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  const loadAll = async () => {
    const hasMoreCache = cacheList?.nextOffset != null;
    const hasMoreGmail = !!nextPage;
    if (!hasMoreCache && !hasMoreGmail) return;
    setLoading(true);
    setErr(null);
    try {
      if (hasMoreCache && cacheList) {
        let off: number | null = cacheList.nextOffset;
        let all = [...messages];
        const gb = cacheList.group_by;
        const k = cacheList.key;
        while (off != null) {
          const params = new URLSearchParams({
            group_by: gb,
            key: k,
            limit: String(GMAIL_LIST_LIMIT),
            offset: String(off),
          });
          const data = await api<CacheListResp>("/api/cache/messages?" + params.toString());
          const chunk = data.messages || [];
          all = [...all, ...chunk];
          off = data.next_offset ?? null;
          setMessages(all);
          setCacheList({
            group_by: data.group_by,
            key: data.key,
            total: data.total,
            nextOffset: off,
          });
        }
        return;
      }
      if (hasMoreGmail && nextPage) {
        let token: string | undefined = nextPage;
        let all = [...messages];
        const q = compiledQ;
        while (token) {
          const pageParams = new URLSearchParams();
          pageParams.set("q", q);
          pageParams.set("page_token", token);
          pageParams.set("limit", String(GMAIL_LIST_LIMIT));
          const pageData: ListResp = await api<ListResp>("/api/messages?" + pageParams.toString());
          const chunk = pageData.messages || [];
          all = [...all, ...chunk];
          token = pageData.nextPageToken;
          setMessages(all);
          setNextPage(token);
        }
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  const startSync = async (full: boolean) => {
    setErr(null);
    try {
      const q = full ? "" : await compileQ();
      const data = await api<{ job_id: string }>("/api/sync/start", {
        method: "POST",
        body: JSON.stringify({
          q: full ? "" : q,
          ...syncRate,
        }),
      });
      setJobId(data.job_id);
    } catch (e) {
      setErr(String(e));
    }
  };

  const refreshAggregates = useCallback(async () => {
    try {
      const data = await api<AggResp>(
        "/api/aggregates?group_by=" +
          encodeURIComponent(aggGroup) +
          "&top_n=0",
      );
      setAgg(data);
    } catch {
      setAgg(null);
    }
  }, [aggGroup]);

  useEffect(() => {
    void refreshAggregates();
  }, [refreshAggregates]);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let intervalId: ReturnType<typeof setInterval> | undefined;
    const poll = () => {
      api<JobPub>("/api/jobs/" + jobId)
        .then((j) => {
          if (cancelled) return;
          setJob(j);
          if (["completed", "cancelled", "failed"].includes(j.status)) {
            if (intervalId) clearInterval(intervalId);
            const pending = bulkPendingRef.current;
            if (pending && pending.jobId === j.id) {
              if (
                j.status === "completed" &&
                (pending.action === "trash" || pending.action === "archive")
              ) {
                const idSet = new Set(pending.ids);
                setMessages((rows) => rows.filter((row) => !idSet.has(row.id)));
                setSelected((s) => {
                  const n = new Set(s);
                  pending.ids.forEach((id) => n.delete(id));
                  return n;
                });
                setPreviewId((pid) => (pid && idSet.has(pid) ? null : pid));
                setPreviewDoc(null);
                setPreviewMeta(null);
              }
              bulkPendingRef.current = null;
            }
            if (j.status === "completed") {
              void refreshAggregates();
            }
          }
        })
        .catch(() => {
          if (intervalId) clearInterval(intervalId);
        });
    };
    void poll();
    intervalId = setInterval(poll, 1200);
    return () => {
      cancelled = true;
      if (intervalId) clearInterval(intervalId);
    };
  }, [jobId, refreshAggregates]);

  const cancelJob = async () => {
    if (!jobId) return;
    try {
      await api("/api/jobs/" + jobId + "/cancel", { method: "POST" });
    } catch {
      /* ignore */
    }
  };

  const resizeBubbleChart = useCallback(() => {
    echartsInstRef.current?.resize();
  }, []);

  useEffect(() => {
    const onFs = () => {
      setChartFullscreen(!!document.fullscreenElement);
      queueMicrotask(resizeBubbleChart);
    };
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, [resizeBubbleChart]);

  useEffect(() => {
    if (aggViewMode !== "chart") return;
    const el = chartWrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => resizeBubbleChart());
    ro.observe(el);
    return () => ro.disconnect();
  }, [aggViewMode, resizeBubbleChart]);

  useEffect(() => {
    window.addEventListener("resize", resizeBubbleChart);
    return () => window.removeEventListener("resize", resizeBubbleChart);
  }, [resizeBubbleChart]);

  useEffect(() => {
    if (aggViewMode === "chart") {
      queueMicrotask(() => resizeBubbleChart());
    }
  }, [aggViewMode, resizeBubbleChart]);

  const toggleChartFullscreen = useCallback(async () => {
    const shell = chartShellRef.current;
    if (!shell) return;
    try {
      if (!document.fullscreenElement) {
        await shell.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch {
      /* ignore */
    }
    queueMicrotask(resizeBubbleChart);
  }, [resizeBubbleChart]);

  const loadPreview = async (id: string) => {
    setPreviewId(id);
    setPreviewDoc(null);
    setPreviewMeta(null);
    try {
      const res = await api<PreviewResp>("/api/messages/" + encodeURIComponent(id));
      setPreviewMeta({
        subject: res.preview.subject,
        from: res.preview.from,
        to: res.preview.to,
        date: res.preview.date,
      });
      setPreviewDoc(res.preview.html_document);
    } catch (e) {
      setPreviewDoc(
        _wrapEmailHtmlErr(`Could not load message: ${String(e)}`),
      );
    }
  };

  function _wrapEmailHtmlErr(msg: string): string {
    const esc = msg.replace(/&/g, "&amp;").replace(/</g, "&lt;");
    return `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><p>${esc}</p></body></html>`;
  }

  const toggleSel = (id: string) => {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };

  const selectAllPage = () => {
    setSelected(new Set(messages.map((m) => m.id)));
  };

  const unselectAll = () => {
    setSelected(new Set());
  };

  const messageById = useMemo(
    () => new Map(messages.map((m) => [m.id, m])),
    [messages],
  );

  const selectedStarredCount = useMemo(() => {
    let n = 0;
    for (const id of selected) {
      if (messageById.get(id)?.starred) n++;
    }
    return n;
  }, [selected, messageById]);

  const selectedImportantCount = useMemo(() => {
    let n = 0;
    for (const id of selected) {
      if (messageById.get(id)?.important) n++;
    }
    return n;
  }, [selected, messageById]);

  const unselectStarred = useCallback(() => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of prev) {
        if (messageById.get(id)?.starred) next.delete(id);
      }
      return next;
    });
  }, [messageById]);

  const unselectImportant = useCallback(() => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of prev) {
        if (messageById.get(id)?.important) next.delete(id);
      }
      return next;
    });
  }, [messageById]);

  const bulk = async (action: "archive" | "trash" | "read" | "unread") => {
    const ids = [...selected];
    if (!ids.length) return;
    setErr(null);
    try {
      if (action === "trash") {
        const data = await api<{ job_id: string; queued: number }>("/api/messages/trash/queue", {
          method: "POST",
          body: JSON.stringify({ message_ids: ids }),
        });
        const prev = bulkPendingRef.current;
        if (prev && prev.action === "trash" && prev.jobId === data.job_id) {
          bulkPendingRef.current = {
            jobId: data.job_id,
            action: "trash",
            ids: [...new Set([...prev.ids, ...ids])],
          };
        } else {
          bulkPendingRef.current = { jobId: data.job_id, action: "trash", ids };
        }
        setJobId(data.job_id);
        return;
      }
      const data = await api<{ job_id: string }>("/api/messages/bulk", {
        method: "POST",
        body: JSON.stringify({ message_ids: ids, action }),
      });
      bulkPendingRef.current = { jobId: data.job_id, action, ids };
      setJobId(data.job_id);
    } catch (e) {
      setErr(String(e));
    }
  };

  const confirmTrash = () => {
    const ids = [...selected];
    if (!ids.length) return;
    let flagged = 0;
    for (const id of ids) {
      const m = messageById.get(id);
      if (m?.starred || m?.important) flagged++;
    }
    if (flagged > 0) {
      const ok = window.confirm(
        `Warning: ${flagged} of the selected message(s) are starred and/or marked important. Moving them to Trash will remove them from your inbox view. Continue?`,
      );
      if (!ok) return;
    }
    void bulk("trash");
  };

  const applyBubbleFilter = async (idx: number) => {
    if (!agg?.items[idx]) return;
    const item = agg.items[idx];
    if (aggGroup === "domain" && item.key === "(unknown)") return;
    if (aggGroup === "age" && item.key === "unknown") return;

    setRawMode(true);
    setErr(null);
    setLoading(true);
    setCacheList(null);
    setNextPage(undefined);

    if (aggGroup === "domain") setRawText(`from:@${item.key}`);
    else if (aggGroup === "sender") setRawText(`from:${item.key}`);
    else setRawText(item.key);
    setCompiledQ(`(cached ${aggGroup}: ${item.key})`);

    try {
      const params = new URLSearchParams({
        group_by: aggGroup,
        key: item.key,
        limit: String(GMAIL_LIST_LIMIT),
        offset: "0",
      });
      const data = await api<CacheListResp>("/api/cache/messages?" + params.toString());
      setMessages(data.messages || []);
      setSelected(new Set());
      setCacheList({
        group_by: data.group_by,
        key: data.key,
        total: data.total,
        nextOffset: data.next_offset,
      });
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  };

  const chartOption = useMemo(() => {
    const labelColor = theme === "dark" ? "#e8eaed" : "#333";
    const emptyTitleColor = theme === "dark" ? "#9aa0a6" : "#666";
    if (!agg?.items.length) {
      return {
        backgroundColor: "transparent",
        title: {
          text: "No cached data — run Sync",
          left: "center",
          textStyle: { color: emptyTitleColor, fontSize: 14 },
        },
      };
    }
    const items = agg.items;
    const maxC = Math.max(...items.map((i) => i.count), 1);
    const nodes = items.map((it, idx) => {
      const short =
        it.key.length > 32 ? `${it.key.slice(0, 30)}…` : it.key;
      return {
        id: String(idx),
        name: short,
        value: it.count,
        fullName: it.key,
        idx,
        category: 0,
        symbolSize: 18 + Math.sqrt(it.count / maxC) * 56,
      };
    });
    return {
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item",
        confine: true,
        backgroundColor: theme === "dark" ? "#35363a" : undefined,
        borderColor: theme === "dark" ? "#5f6368" : undefined,
        textStyle: { color: labelColor },
        formatter: (params: {
          data?: { fullName?: string; value?: number };
          name?: string;
          value?: number | string;
        }) => {
          const d = params.data;
          const title = (d?.fullName || params.name || "").trim();
          const n = d?.value ?? params.value;
          const count = typeof n === "number" ? n : Number(n);
          if (!title) return "";
          return `${title}\n${Number.isFinite(count) ? count : n} messages`;
        },
      },
      animationDuration: 600,
      series: [
        {
          type: "graph",
          layout: "force",
          roam: true,
          draggable: true,
          width: "92%",
          height: "88%",
          center: ["50%", "52%"],
          data: nodes,
          links: [],
          categories: [{ name: "groups" }],
          label: {
            show: true,
            fontSize: 10,
            color: labelColor,
            formatter: "{c}",
          },
          lineStyle: { color: "source", opacity: 0 },
          emphasis: {
            focus: "adjacency",
            scale: true,
            label: { fontSize: 11 },
          },
          force: {
            repulsion: 420,
            gravity: 0.08,
            friction: 0.55,
            edgeLength: [60, 120],
            layoutAnimation: true,
          },
        },
      ],
    };
  }, [agg, theme]);

  const logout = async () => {
    await api("/api/auth/logout", { method: "POST" });
    refreshAuth();
  };

  const setField = (key: string, value: string | boolean) => {
    setStructured((s) => ({ ...s, [key]: value }));
  };

  const hasMoreResults =
    !!nextPage || (cacheList != null && cacheList.nextOffset != null);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Gmail Filter (local)</h1>
        <div className="topbar-actions">
          <label className="theme-toggle">
            <input
              type="checkbox"
              checked={theme === "dark"}
              onChange={(e) => setTheme(e.target.checked ? "dark" : "light")}
            />
            <span>Dark</span>
          </label>
          {auth?.authenticated ? (
            <>
              <span className="muted">Signed in</span>
              <button type="button" className="btn" onClick={() => void logout()}>
                Sign out
              </button>
            </>
          ) : (
            <a className="btn btn-primary" href="/api/auth/google">
              Sign in with Google
            </a>
          )}
        </div>
      </header>

      {!auth?.authenticated && (
        <p className="muted">
          Connect your Google account (Gmail API). Configure OAuth in <code>.env</code> first.
        </p>
      )}

      <div className="layout-search-aggregates">
        <div className="panel panel-search">
        <h2>Search</h2>
        <label className="field" style={{ marginBottom: "0.75rem" }}>
          <span>Mode</span>
          <select
            value={rawMode ? "raw" : "form"}
            onChange={(e) => setRawMode(e.target.value === "raw")}
          >
            <option value="form">Advanced search (same fields as Gmail)</option>
            <option value="raw">Raw Gmail query</option>
          </select>
        </label>

        {rawMode ? (
          <label className="field">
            <span>Query (paste from Gmail search box)</span>
            <textarea
              rows={3}
              value={rawText}
              onChange={(e) => setRawText(e.target.value)}
              placeholder='e.g. category:promotions older_than:1y'
            />
          </label>
        ) : (
          <div className="grid">
            <label className="field">
              <span>From</span>
              <input
                value={(structured.from_addr as string) || ""}
                onChange={(e) => setField("from_addr", e.target.value)}
              />
            </label>
            <label className="field">
              <span>To</span>
              <input
                value={(structured.to_addr as string) || ""}
                onChange={(e) => setField("to_addr", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Subject</span>
              <input
                value={(structured.subject as string) || ""}
                onChange={(e) => setField("subject", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Has the words</span>
              <input
                value={(structured.has_words as string) || ""}
                onChange={(e) => setField("has_words", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Doesn&apos;t have</span>
              <input
                value={(structured.not_have as string) || ""}
                onChange={(e) => setField("not_have", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Cc</span>
              <input
                value={(structured.cc as string) || ""}
                onChange={(e) => setField("cc", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Bcc</span>
              <input
                value={(structured.bcc as string) || ""}
                onChange={(e) => setField("bcc", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Delivered to</span>
              <input
                value={(structured.deliveredto as string) || ""}
                onChange={(e) => setField("deliveredto", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Label</span>
              <input
                value={(structured.label as string) || ""}
                onChange={(e) => setField("label", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Category</span>
              <select
                value={(structured.category as string) || ""}
                onChange={(e) => setField("category", e.target.value)}
              >
                {CATEGORIES.map((c) => (
                  <option key={c || "none"} value={c}>
                    {c || "(any)"}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Filename</span>
              <input
                value={(structured.filename as string) || ""}
                onChange={(e) => setField("filename", e.target.value)}
              />
            </label>
            <label className="field">
              <span>RFC822 Message-ID</span>
              <input
                value={(structured.rfc822msgid as string) || ""}
                onChange={(e) => setField("rfc822msgid", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Larger than</span>
              <input
                placeholder="e.g. 5M"
                value={(structured.larger as string) || ""}
                onChange={(e) => setField("larger", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Smaller than</span>
              <input
                placeholder="e.g. 10M"
                value={(structured.smaller as string) || ""}
                onChange={(e) => setField("smaller", e.target.value)}
              />
            </label>
            <label className="field">
              <span>After</span>
              <input
                placeholder="YYYY/MM/DD"
                value={(structured.after as string) || ""}
                onChange={(e) => setField("after", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Before</span>
              <input
                placeholder="YYYY/MM/DD"
                value={(structured.before as string) || ""}
                onChange={(e) => setField("before", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Older than</span>
              <input
                placeholder="e.g. 1y"
                value={(structured.older_than as string) || ""}
                onChange={(e) => setField("older_than", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Newer than</span>
              <input
                placeholder="e.g. 6m"
                value={(structured.newer_than as string) || ""}
                onChange={(e) => setField("newer_than", e.target.value)}
              />
            </label>
            <label className="field">
              <span>Search in</span>
              <select
                value={(structured.search_in as string) || "anywhere"}
                onChange={(e) => setField("search_in", e.target.value)}
              >
                {SEARCH_IN.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Read state</span>
              <select
                value={readFilter}
                onChange={(e) => setReadFilter(e.target.value as "any" | "read" | "unread")}
              >
                <option value="any">Any</option>
                <option value="read">Read</option>
                <option value="unread">Unread</option>
              </select>
            </label>
            <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={!!structured.has_attachment}
                onChange={(e) => setField("has_attachment", e.target.checked)}
              />
              <span>Has attachment</span>
            </label>
            <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: "0.5rem" }}>
              <input
                type="checkbox"
                checked={!!structured.exclude_chats}
                onChange={(e) => setField("exclude_chats", e.target.checked)}
              />
              <span>Don&apos;t include chats</span>
            </label>
          </div>
        )}

        <p className="muted" style={{ marginTop: "0.75rem" }}>
          Gmail sync rate (list page size, parallelism, chunk size, pauses). Applies to{" "}
          <em>Sync cache</em> and <em>Load all mail</em>.
        </p>
        <div className="grid" style={{ marginTop: "0.35rem" }}>
          <label className="field">
            List page size (max 500)
            <input
              type="number"
              min={1}
              max={500}
              step={1}
              value={syncRate.gmail_list_page_size}
              onChange={(e) => {
                const v = parseInt(e.target.value, 10);
                if (!Number.isNaN(v))
                  setSyncRate((s) => ({ ...s, gmail_list_page_size: v }));
              }}
            />
          </label>
          <label className="field">
            Parallel workers
            <input
              type="number"
              min={1}
              max={128}
              step={1}
              value={syncRate.gmail_parallel_workers}
              onChange={(e) => {
                const v = parseInt(e.target.value, 10);
                if (!Number.isNaN(v))
                  setSyncRate((s) => ({ ...s, gmail_parallel_workers: v }));
              }}
            />
          </label>
          <label className="field">
            Metadata chunk size
            <input
              type="number"
              min={1}
              max={200}
              step={1}
              value={syncRate.gmail_enrich_chunk_size}
              onChange={(e) => {
                const v = parseInt(e.target.value, 10);
                if (!Number.isNaN(v))
                  setSyncRate((s) => ({ ...s, gmail_enrich_chunk_size: v }));
              }}
            />
          </label>
          <label className="field">
            Pause after chunk (s)
            <input
              type="number"
              min={0}
              max={60}
              step={0.05}
              value={syncRate.gmail_sync_chunk_pause_seconds}
              onChange={(e) => {
                const v = parseFloat(e.target.value);
                if (!Number.isNaN(v))
                  setSyncRate((s) => ({ ...s, gmail_sync_chunk_pause_seconds: v }));
              }}
            />
          </label>
          <label className="field">
            Pause after list page (s)
            <input
              type="number"
              min={0}
              max={60}
              step={0.05}
              value={syncRate.gmail_list_page_pause_seconds}
              onChange={(e) => {
                const v = parseFloat(e.target.value);
                if (!Number.isNaN(v))
                  setSyncRate((s) => ({ ...s, gmail_list_page_pause_seconds: v }));
              }}
            />
          </label>
          <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: "0.5rem" }}>
            <input
              type="checkbox"
              checked={syncRate.gmail_adaptive_sync}
              onChange={(e) =>
                setSyncRate((s) => ({ ...s, gmail_adaptive_sync: e.target.checked }))
              }
            />
            <span>Adaptive sync (ramp up until rate-limited, then back off)</span>
          </label>
        </div>
        <p className="muted small" style={{ marginTop: "0.35rem" }}>
          Estimated Gmail API request rate: ~{estimatedSyncRpm} requests/min (from your pauses and chunk
          size; assumes ~0.35s wall time per metadata chunk).
          {syncRate.gmail_adaptive_sync
            ? " Adaptive mode may exceed this until limits are hit."
            : ""}
        </p>

        <div className="row-actions">
          <button type="button" className="btn btn-primary" disabled={!auth?.authenticated || loading} onClick={() => void runSearch()}>
            Search Gmail
          </button>
          <button type="button" className="btn" disabled={!auth?.authenticated || loading} onClick={() => void compileQ()}>
            Compile query only
          </button>
          <button type="button" className="btn" disabled={!auth?.authenticated || loading} onClick={() => void startSync(false)}>
            Sync cache (current query)
          </button>
          <button
            type="button"
            className="btn"
            disabled={!auth?.authenticated || loading}
            onClick={() => void startSync(true)}
            title="Indexes all mail in Gmail (query in:anywhere). Can take a long time."
          >
            Load all mail (full sync)
          </button>
        </div>
        <p className="muted" style={{ marginTop: "0.5rem" }}>
          Compiled <code>q</code> (used for Search &amp; sync):
        </p>
        <div className="compiled">{compiledQ || "(run compile or search)"}</div>
        </div>

        <div className="panel panel-chart panel-aggregates">
          <div className="chart-shell" ref={chartShellRef}>
            <div className="chart-toolbar">
              <h2 className="chart-title">Aggregates (cached sync)</h2>
              <div className="chart-view-toggle" role="group" aria-label="Aggregate view">
                <button
                  type="button"
                  className={`btn btn-toggle${aggViewMode === "chart" ? " active" : ""}`}
                  onClick={() => setAggViewMode("chart")}
                >
                  Bubble chart
                </button>
                <button
                  type="button"
                  className={`btn btn-toggle${aggViewMode === "list" ? " active" : ""}`}
                  onClick={() => setAggViewMode("list")}
                >
                  List
                </button>
              </div>
              <label className="field chart-field">
                <span>Group by</span>
                <select value={aggGroup} onChange={(e) => setAggGroup(e.target.value as typeof aggGroup)}>
                  <option value="domain">Domain</option>
                  <option value="sender">Sender</option>
                  <option value="age">Age</option>
                  <option value="newsletter">Newsletter (heuristic)</option>
                </select>
              </label>
              <span className="muted chart-stat">
                Cached: {agg?.cached_total ?? "—"} · {agg?.items.length ?? "—"} groups
              </span>
              <button
                type="button"
                className="btn chart-fs-btn"
                disabled={aggViewMode === "list"}
                title={aggViewMode === "list" ? "Fullscreen is for the bubble chart" : undefined}
                onClick={() => void toggleChartFullscreen()}
              >
                {chartFullscreen ? "Exit fullscreen" : "Fullscreen"}
              </button>
            </div>
            {aggViewMode === "chart" ? (
              <div className="chart-wrap" ref={chartWrapRef}>
                <ReactECharts
                  option={chartOption}
                  style={{ width: "100%", height: "100%" }}
                  notMerge
                  lazyUpdate
                  opts={{ renderer: "canvas" }}
                  onChartReady={(inst) => {
                    echartsInstRef.current = inst;
                    inst.resize();
                  }}
                  onEvents={{
                    click: (p: {
                      dataType?: string;
                      data?: { idx?: number };
                      dataIndex?: number;
                    }) => {
                      if (p.dataType === "node" && p.data?.idx != null) {
                        void applyBubbleFilter(p.data.idx);
                      } else if (p.dataIndex != null) {
                        void applyBubbleFilter(p.dataIndex);
                      }
                    },
                  }}
                />
              </div>
            ) : (
              <div className="agg-list-wrap">
                {!agg?.items.length ? (
                  <p className="muted agg-list-empty">No cached data — run Sync</p>
                ) : (
                  <ul className="agg-list">
                    {agg.items.map((it, idx) => (
                      <li key={`${it.key}-${idx}`}>
                        <button
                          type="button"
                          className="agg-list-row"
                          onClick={() => void applyBubbleFilter(idx)}
                        >
                          <span className="agg-list-key" title={it.key}>
                            {it.key.length > 56 ? `${it.key.slice(0, 54)}…` : it.key}
                          </span>
                          <span className="agg-list-count">{it.count.toLocaleString()}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            <p className="muted chart-hint">
              {aggViewMode === "chart"
                ? "All groups in the cache are shown (sorted by count). Hover for labels. Click a bubble to list that bucket. Drag or zoom in fullscreen. Very large lists may be slow."
                : "All groups are listed (sorted by count). Click a row to load messages for that group from the cache."}
            </p>
          </div>
        </div>
      </div>

      {job && (
        <div className="panel">
          <h2>Job</h2>
          <div className="muted">
            {job.kind} — {job.status} {job.phase ? `(${job.phase})` : ""}
          </div>
          <div>{job.message}</div>
          {job.status === "running" && job.kind === "sync" && job.requests_per_minute != null && (
            <div className="muted small">
              Measured API rate (rolling 60s): ~{Math.round(job.requests_per_minute)} requests/min
              {job.adaptive_workers != null && job.adaptive_pause_seconds != null && (
                <>
                  {" "}
                  — adaptive: {job.adaptive_workers} workers,{" "}
                  {job.adaptive_pause_seconds.toFixed(2)}s chunk pause
                </>
              )}
            </div>
          )}
          {job.status === "running" &&
            job.total_hint != null &&
            job.total_hint > 0 &&
            job.processed >= 0 && (
              <div className="muted small">
                Progress: {job.processed.toLocaleString()} / {job.total_hint.toLocaleString()} messages
              </div>
            )}
          {job.error && <div className="job-err">{job.error}</div>}
          {job.kind === "trash_queue" && job.status === "running" && (job.queue_remaining ?? 0) > 0 && (
            <div className="muted small">
              Waiting in queue: {(job.queue_remaining ?? 0).toLocaleString()} message(s)
            </div>
          )}
          {job.kind === "trash_queue" &&
            job.deleted_recent &&
            job.deleted_recent.length > 0 && (
              <div className="trash-deleted-log">
                <div className="muted small">Deleted (subjects from cache; newest at bottom)</div>
                <ul className="trash-deleted-list">
                  {job.deleted_recent.map((d) => (
                    <li key={d.id} className="trash-deleted-item" title={d.subject}>
                      <span className="muted">{d.id.slice(0, 12)}…</span> {d.subject}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          {job.status === "running" && job.percent != null && (
            <div className="progress">
              <div className="progress-bar">
                <div style={{ width: `${Math.min(100, job.percent)}%` }} />
              </div>
            </div>
          )}
          {job.status === "running" &&
            job.percent == null &&
            !job.kind.startsWith("bulk") &&
            job.kind !== "trash_queue" && (
            <div className="progress">
              <div className="progress-bar progress-bar-indeterminate" />
            </div>
          )}
          {job.status === "running" && (
            <div className="row-actions">
              <button type="button" className="btn btn-danger" onClick={() => void cancelJob()}>
                Force cancel
              </button>
            </div>
          )}
        </div>
      )}

      <div className="panel panel-results">
        <h2>
          Results (
          {cacheList
            ? `${messages.length} / ${cacheList.total} cached`
            : messages.length}
          )
        </h2>
        <p className="muted small results-hint">
          Live search hides messages that are already in Gmail Trash (so trashed mail won’t show up again
          here). Charts use the local cache, which updates after trash/archive jobs finish.
        </p>
        <div className="results-toolbar">
          {hasMoreResults && (
            <>
              <button type="button" className="btn" disabled={loading} onClick={() => void loadMore()}>
                Load more
              </button>
              <button type="button" className="btn" disabled={loading} onClick={() => void loadAll()}>
                Load all
              </button>
            </>
          )}
        </div>
        <div className="row-actions">
            <button type="button" className="btn" onClick={selectAllPage}>
              Select all on page
            </button>
            <button type="button" className="btn" disabled={!selected.size} onClick={unselectAll}>
              Unselect all
            </button>
            <button
              type="button"
              className="btn"
              disabled={!selectedStarredCount}
              title="Remove starred messages from the current selection"
              onClick={unselectStarred}
            >
              Unselect starred
            </button>
            <button
              type="button"
              className="btn"
              disabled={!selectedImportantCount}
              title="Remove important messages from the current selection"
              onClick={unselectImportant}
            >
              Unselect important
            </button>
            <button
              type="button"
              className="btn"
              disabled={!selected.size}
              onClick={() => void bulk("archive")}
            >
              Archive selected
            </button>
            <button
              type="button"
              className="btn btn-danger"
              disabled={!selected.size}
              onClick={confirmTrash}
            >
              Trash selected
            </button>
            <button
              type="button"
              className="btn"
              disabled={!selected.size}
              onClick={() => void bulk("read")}
            >
              Mark read
            </button>
            <button
              type="button"
              className="btn"
              disabled={!selected.size}
              onClick={() => void bulk("unread")}
            >
              Mark unread
            </button>
          </div>
        {err && <p className="results-err">{err}</p>}
        <div className="layout-results-preview">
          <div className="results-main">
            <div className="results-scroll">
              <table className="msg-table msg-table-wide">
                <thead>
                  <tr>
                    <th className="col-num" scope="col">
                      #
                    </th>
                    <th className="col-check" scope="col">
                      <span className="sr-only">Select</span>
                    </th>
                    <th className="col-flags" scope="col" title="Starred (★) and Important (!)">
                      Flags
                    </th>
                    <th className="col-dir" scope="col" title="Sent vs received (inbox)">
                      Sent / Recv
                    </th>
                    <th>From</th>
                    <th className="col-date">Date</th>
                    <th>Subject</th>
                  </tr>
                </thead>
                <tbody>
                  {messages.map((m, idx) => (
                    <tr key={m.id} className={m.unread ? "msg-row-unread" : undefined}>
                      <td className="col-num muted">{idx + 1}</td>
                      <td className="col-check">
                        <input
                          type="checkbox"
                          checked={selected.has(m.id)}
                          onChange={() => toggleSel(m.id)}
                          aria-label={`Select row ${idx + 1}`}
                        />
                      </td>
                      <td className="col-flags">
                        {m.starred ? (
                          <span className="msg-flag msg-flag-star" title="Starred" aria-label="Starred">
                            ★
                          </span>
                        ) : null}
                        {m.important ? (
                          <span
                            className="msg-flag msg-flag-important"
                            title="Important"
                            aria-label="Important"
                          >
                            !
                          </span>
                        ) : null}
                        {!m.starred && !m.important ? (
                          <span className="muted">—</span>
                        ) : null}
                      </td>
                      <td className="col-dir muted" title={dirHint(m)}>
                        {m.sent ? (
                          <span className="msg-dir msg-dir-sent">Sent</span>
                        ) : m.received ? (
                          <span className="msg-dir msg-dir-received">Received</span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="cell-ellipsis" title={m.from || ""}>
                        {m.from || "—"}
                      </td>
                      <td className="col-date muted" title={formatMsgDate(m.internalDate)}>
                        {formatMsgDate(m.internalDate)}
                      </td>
                      <td className="cell-ellipsis" title={m.subject || ""}>
                        <button
                          type="button"
                          className="linklike"
                          onClick={() => void loadPreview(m.id)}
                        >
                          {m.subject || "(no subject)"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          <aside className="results-preview-col" aria-label="Message preview">
            {previewId ? (
              <div className="preview preview-sidebar">
                <div className="preview-meta">
                  {previewMeta ? (
                    <>
                      <div>
                        <strong>{previewMeta.subject || "(no subject)"}</strong>
                      </div>
                      <div className="muted small">
                        From: {previewMeta.from || "—"}
                      </div>
                      <div className="muted small">
                        To: {previewMeta.to || "—"}
                      </div>
                      <div className="muted small">
                        Date: {previewMeta.date || "—"}
                      </div>
                    </>
                  ) : (
                    <div className="muted">Loading…</div>
                  )}
                </div>
                <iframe
                  title="Email body"
                  className="preview-frame"
                  sandbox=""
                  srcDoc={previewDoc || "<!DOCTYPE html><html><body><p>Loading…</p></body></html>"}
                />
              </div>
            ) : (
              <div className="preview-placeholder muted">
                Click a subject to load a preview here.
              </div>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}
