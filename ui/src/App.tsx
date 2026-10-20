import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import type { MachinesResponse, Machine, RpcCluster, UsageStats, MediaModel, VideoGenResponse, ImageGenJob, JobProgress, MediaJob, MediaJobsResponse } from "./types";
import { MachineModels, MachineStatus, ReplicaOverview } from "./MachineActivity";
import { machineCluster } from "./machine-state";
import { requestChatCompletion, type ChatPhase } from "./chat-request";
import { deploymentUrls } from "./deployment";

const POLL_MS = 3000;
const LS_KEY  = "swarm_admin_key";

function useLocalStorage<T>(key: string, init: T): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try { const s = localStorage.getItem(key); return s ? JSON.parse(s) as T : init; }
    catch { return init; }
  });
  useEffect(() => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
  }, [key, value]);
  return [value, setValue];
}

function apiFetch(path: string, key: string) {
  return fetch(`/swarm/api${path}`, { headers: { "X-API-Key": key } });
}

// ── login screen ──────────────────────────────────────────────────────────────

const deployment = deploymentUrls(window.location.origin);
const COORDINATOR_URL = deployment.api;
const AGENT_DOWNLOAD_URL = deployment.agent;

const JOIN_COMMANDS: Record<string, { label: string; cmd: string }[]> = {
  windows: [
    {
      label: "CMD — download & install",
      cmd: `curl.exe -fsSL ${AGENT_DOWNLOAD_URL} -o agent.py && python agent.py --coordinator ${COORDINATOR_URL} --api-key YOUR_KEY`,
    },
    {
      label: "PowerShell — download & install",
      cmd: `$ErrorActionPreference = 'Stop'; Invoke-WebRequest -Uri '${AGENT_DOWNLOAD_URL}' -OutFile agent.py; python agent.py --coordinator ${COORDINATOR_URL} --api-key YOUR_KEY`,
    },
  ],
  linux: [
    {
      label: "Bash — download & run",
      cmd: `curl -fsSL ${AGENT_DOWNLOAD_URL} -o agent.py && python3 agent.py --coordinator ${COORDINATOR_URL} --api-key YOUR_KEY`,
    },
  ],
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }
  return (
    <button onClick={copy} style={{
      position: "absolute", top: 8, right: 8,
      background: copied ? "rgba(52,211,153,0.18)" : "rgba(255,255,255,0.07)",
      border: `1px solid ${copied ? "rgba(52,211,153,0.4)" : "rgba(255,255,255,0.12)"}`,
      borderRadius: 6, color: copied ? "var(--green)" : "var(--muted)",
      fontSize: 10, fontWeight: 700, padding: "3px 9px", cursor: "pointer",
      letterSpacing: "0.06em", transition: "all 0.15s",
    }}>
      {copied ? "COPIED" : "COPY"}
    </button>
  );
}

function JoinCard() {
  const [os, setOs] = useState<"windows" | "linux">("windows");
  const cmds = JOIN_COMMANDS[os];

  return (
    <div style={{
      background: "rgba(255,255,255,0.03)",
      backdropFilter: "blur(20px)", WebkitBackdropFilter: "blur(20px)",
      border: "1px solid rgba(255,255,255,0.09)",
      boxShadow: "0 24px 64px rgba(0,0,0,0.4)",
      borderRadius: 18, padding: "32px 36px", width: 460,
      animation: "fadeUp 0.5s ease",
    }}>
      <div style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 4, color: "var(--text)" }}>
          Connect a machine
        </div>
        <div style={{ fontSize: 12, color: "var(--muted)" }}>
          Run on any machine with a GPU to join the swarm
        </div>
      </div>

      {/* OS toggle */}
      <div style={{
        display: "inline-flex", gap: 3, marginBottom: 18, padding: "3px",
        background: "rgba(255,255,255,0.04)", borderRadius: 9,
        border: "1px solid rgba(255,255,255,0.07)",
      }}>
        {(["windows", "linux"] as const).map(o => (
          <button key={o} onClick={() => setOs(o)} style={{
            padding: "4px 14px", borderRadius: 7, fontSize: 11, fontWeight: 600,
            border: "none", cursor: "pointer",
            background: os === o
              ? "rgba(167,139,250,0.18)"
              : "transparent",
            color: os === o ? "var(--accent)" : "var(--muted)",
            transition: "all 0.12s",
          }}>
            {o === "windows" ? "Windows" : "Linux / macOS"}
          </button>
        ))}
      </div>

      {/* command blocks */}
      {cmds.map(({ label, cmd }) => (
        <div key={label} style={{ marginBottom: 12 }}>
          <div style={{
            fontSize: 9, color: "var(--muted)", textTransform: "uppercase",
            letterSpacing: "0.1em", fontWeight: 700, marginBottom: 6,
          }}>{label}</div>
          <div style={{ position: "relative" }}>
            <pre style={{
              margin: 0, padding: "12px 80px 12px 14px",
              background: "rgba(0,0,0,0.35)",
              border: "1px solid rgba(255,255,255,0.08)",
              borderRadius: 10, fontFamily: "var(--mono)", fontSize: 11,
              color: "var(--text)", whiteSpace: "pre-wrap", wordBreak: "break-all",
              lineHeight: 1.6,
            }}>{cmd}</pre>
            <CopyButton text={cmd} />
          </div>
        </div>
      ))}

      <div style={{
        marginTop: 16, paddingTop: 14,
        borderTop: "1px solid rgba(255,255,255,0.06)",
      }}>
        <div style={{ fontSize: 9, color: "var(--muted)", textTransform: "uppercase",
          letterSpacing: "0.1em", fontWeight: 700, marginBottom: 3 }}>Coordinator</div>
        <code style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--accent)" }}>
          {COORDINATOR_URL}
        </code>
      </div>
    </div>
  );
}

function LoginScreen({ onLogin }: { onLogin: (key: string) => void }) {
  const [key, setKey]     = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy]   = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!key.trim()) return;
    setBusy(true);
    setError("");
    try {
      const res = await apiFetch("/machines", key.trim());
      if (res.ok) {
        localStorage.setItem(LS_KEY, key.trim());
        onLogin(key.trim());
      } else {
        setError("Invalid key — try again.");
      }
    } catch {
      setError("Cannot reach coordinator. Is it running?");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center",
      justifyContent: "center", flexWrap: "wrap", gap: 24, padding: 32,
    }}>
      <div style={{
        background: "rgba(255,255,255,0.04)",
        backdropFilter: "blur(20px)", WebkitBackdropFilter: "blur(20px)",
        border: "1px solid rgba(167,139,250,0.2)",
        boxShadow: "0 0 0 1px rgba(167,139,250,0.08), 0 24px 64px rgba(0,0,0,0.5)",
        borderRadius: 18, padding: "44px 52px", width: 380,
        animation: "fadeUp 0.4s ease",
      }}>
        <div style={{ marginBottom: 28 }}>
          <h1 style={{
            fontSize: 22, fontWeight: 700, marginBottom: 4, letterSpacing: "-0.03em",
            background: "linear-gradient(135deg, #a78bfa 0%, #22d3ee 100%)",
            WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
          }}>
            swarm-llm
          </h1>
          <p style={{ color: "var(--muted)", fontSize: 13 }}>
            control plane&nbsp;&nbsp;·&nbsp;&nbsp;
            <a href={AGENT_DOWNLOAD_URL}
              style={{ color: "var(--accent)", textDecoration: "none", fontSize: 12, opacity: 0.8 }}>
              Python agent ↗
            </a>
          </p>
        </div>

        <form onSubmit={handleSubmit}>
          <label style={{ display: "block", color: "var(--muted)", fontSize: 10,
            textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 8, fontWeight: 600 }}>
            Admin key
          </label>
          <input
            type="password"
            value={key}
            onChange={e => setKey(e.target.value)}
            placeholder="••••••••••••"
            autoFocus
            style={{
              width: "100%", padding: "11px 14px",
              background: "rgba(255,255,255,0.05)",
              border: `1px solid ${error ? "var(--red)" : "rgba(255,255,255,0.1)"}`,
              borderRadius: 9, color: "var(--text)", fontSize: 14,
              fontFamily: "var(--mono)", outline: "none", boxSizing: "border-box",
              transition: "border-color 0.15s, box-shadow 0.15s",
              boxShadow: error ? "0 0 0 3px rgba(248,113,113,0.15)" : "none",
            }}
          />
          {error && (
            <p style={{ color: "var(--red)", fontSize: 12, marginTop: 8 }}>{error}</p>
          )}
          <button
            type="submit"
            disabled={busy || !key.trim()}
            style={{
              marginTop: 18, width: "100%", padding: "11px 0",
              background: busy || !key.trim()
                ? "rgba(255,255,255,0.06)"
                : "linear-gradient(135deg, #a78bfa 0%, #818cf8 100%)",
              color: busy || !key.trim() ? "var(--muted)" : "#fff",
              border: "none", borderRadius: 9, fontSize: 14, fontWeight: 600,
              cursor: busy || !key.trim() ? "not-allowed" : "pointer",
              transition: "opacity 0.15s, transform 0.1s",
              letterSpacing: "0.01em",
            }}
          >
            {busy ? "Connecting…" : "Connect →"}
          </button>
        </form>
      </div>

      <JoinCard />
    </div>
  );
}

// ── dashboard sub-components ──────────────────────────────────────────────────

function StatCard({ value, label, color = "var(--accent)", glow = false }: {
  value: string | number; label: string; color?: string; glow?: boolean;
}) {
  const lit = glow && Number(value) > 0;
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
      gap: 6, padding: "18px 20px", flex: "1 1 0", minWidth: 80, position: "relative",
    }}>
      <div style={{
        fontSize: 32, fontWeight: 700, lineHeight: 1, letterSpacing: "-0.04em",
        fontVariantNumeric: "tabular-nums", color,
        textShadow: lit ? `0 0 18px ${color}, 0 0 40px ${color}55` : undefined,
        transition: "text-shadow 0.4s",
      }}>
        {value}
      </div>
      <div style={{
        fontSize: 9, color: lit ? color : "var(--muted)", textTransform: "uppercase",
        letterSpacing: "0.13em", fontWeight: 700, opacity: lit ? 0.75 : 1,
        transition: "color 0.4s",
      }}>
        {label}
      </div>
      {lit && (
        <div style={{
          position: "absolute", inset: 0, borderRadius: "inherit",
          background: `radial-gradient(ellipse at center, ${color}12 0%, transparent 70%)`,
          pointerEvents: "none",
        }} />
      )}
    </div>
  );
}

const td: React.CSSProperties = { padding: "11px 16px", verticalAlign: "middle" };
const th: React.CSSProperties = {
  padding: "10px 16px", textAlign: "left", fontSize: 10,
  color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.09em",
  background: "rgba(255,255,255,0.02)", fontWeight: 700,
  borderBottom: "1px solid rgba(255,255,255,0.07)",
};

function MachineRow({ m, cluster }: { m: Machine; cluster?: RpcCluster }) {
  const gpu     = m.gpu_name ? `${m.gpu_name}${m.gpu_vram_gb ? ` (${m.gpu_vram_gb} GB)` : ""}` : "CPU only";
  const ram     = m.ram_total_gb != null && m.ram_total_gb > 0
    ? `${m.ram_free_gb?.toFixed(1) ?? "—"} / ${m.ram_total_gb.toFixed(1)} GB` : "—";
  const latency = m.avg_response_ms != null ? `${m.avg_response_ms.toFixed(0)} ms` : "—";
  const seen    = m.last_seen_sec < 60 ? `${m.last_seen_sec.toFixed(0)}s ago` : `${(m.last_seen_sec/60).toFixed(1)}m ago`;

  return (
    <tr style={{ borderTop: "1px solid rgba(255,255,255,0.05)", transition: "background 0.12s" }}
      onMouseEnter={e => (e.currentTarget.style.background = "rgba(167,139,250,0.045)")}
      onMouseLeave={e => (e.currentTarget.style.background = "")}>
      <td style={td}><MachineStatus machine={m} cluster={cluster} /></td>
      <td style={td}>
        <div style={{ fontWeight: 600, letterSpacing: "-0.01em" }}>{m.hostname}</div>
        <div style={{ color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 10, marginTop: 2 }}>
          {m.detail?.startsWith("LAN_PROBE ") ? "Checking local connectivity" : m.phase === "DOWNLOADING" ? m.key : m.detail || m.key}
        </div>
        {m.last_error && (
          <details style={{ marginTop: 6, maxWidth: 480, fontSize: 11 }}>
            <summary style={{ cursor: "pointer", color: "var(--red)" }}>
              Last failure · {new Date(m.last_error.received_at * 1000).toLocaleString()}
            </summary>
            <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 320,
              overflow: "auto", color: "var(--text)", fontFamily: "var(--mono)" }}>
              {[m.last_error.detail, m.last_error.traceback, m.last_error.log_tail,
                JSON.stringify(m.last_error.diagnostics, null, 2)].filter(Boolean).join("\n\n")}
            </pre>
          </details>
        )}
      </td>
      <td style={td}>
        <MachineModels machine={m} cluster={cluster} />
      </td>
      <td style={td}>
        <div style={{ fontSize: 13 }}>{m.cpu_name}</div>
        {m.cpu_cores && <div style={{ color: "var(--muted)", fontSize: 11, marginTop: 1 }}>{m.cpu_cores} threads</div>}
      </td>
      <td style={{ ...td, fontFamily: "var(--mono)", fontSize: 12 }}>{ram}</td>
      <td style={td}>
        <span style={{ color: m.gpu_name ? "var(--yellow)" : "var(--muted)", fontSize: 13 }}>{gpu}</span>
        {m.runtime?.gpu_used_mb != null && <div style={{ color: "var(--muted)", fontSize: 11, marginTop: 4 }}>
          GPU memory{m.alive ? "" : " at last report"}: {(m.runtime.gpu_used_mb / 1024).toFixed(1)} GiB
        </div>}
      </td>
      <td style={{ ...td, fontSize: 11 }}>
        {m.media_phase
          ? <span style={{
              color: m.media_phase === "RUNNING" ? "var(--green)"
                   : m.media_phase === "DOWNLOADING_DEPS" ? "var(--yellow)"
                   : "var(--muted)",
              fontFamily: "var(--mono)",
            }}>
              {m.media_phase}{m.media_model ? ` ${m.media_model}` : ""}
            </span>
          : <span style={{ color: "var(--muted)" }}>—</span>}
      </td>
      <td style={{ ...td, fontFamily: "var(--mono)", fontSize: 12 }}>
        {m.network_mbps != null
          ? <span style={{ color: m.network_mbps < 2 ? "var(--red)" : m.network_mbps < 10 ? "var(--yellow)" : "var(--green)" }}>
              {m.network_mbps.toFixed(1)} MB/s
            </span>
          : <span style={{ color: "var(--muted)" }}>—</span>}
      </td>
      <td style={{ ...td, fontFamily: "var(--mono)", fontSize: 12 }}>
        <span style={{ color: "var(--green)" }}>{m.requests_ok}</span>
        <span style={{ color: "var(--muted)" }}> / </span>
        <span style={{ color: m.requests_err > 0 ? "var(--red)" : "var(--muted)" }}>{m.requests_err}</span>
      </td>
      <td style={{ ...td, fontFamily: "var(--mono)", fontSize: 12 }}>{latency}</td>
      <td style={{ ...td, color: m.last_seen_sec > 30 ? "var(--yellow)" : "var(--muted)",
        fontFamily: "var(--mono)", fontSize: 11 }}>{seen}</td>
    </tr>
  );
}

// ── chat ──────────────────────────────────────────────────────────────────────

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

function renderInline(text: string, key: number): React.ReactNode {
  const tokens: { type: string; content: string }[] = [];
  const re = /`([^`]+)`|\*\*\*([^*]+)\*\*\*|\*\*([^*]+)\*\*|__([^_]+)__|\*([^*\n]+)\*|_([^_\n]+)_/g;
  let last = 0; let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) tokens.push({ type: "text", content: text.slice(last, m.index) });
    if      (m[1]) tokens.push({ type: "code",       content: m[1] });
    else if (m[2]) tokens.push({ type: "bolditalic", content: m[2] });
    else if (m[3]) tokens.push({ type: "bold",       content: m[3] });
    else if (m[4]) tokens.push({ type: "bold",       content: m[4] });
    else if (m[5]) tokens.push({ type: "italic",     content: m[5] });
    else if (m[6]) tokens.push({ type: "italic",     content: m[6] });
    last = m.index + m[0].length;
  }
  if (last < text.length) tokens.push({ type: "text", content: text.slice(last) });
  return (
    <span key={key}>
      {tokens.map((t, i) => {
        if (t.type === "code")       return <code key={i} style={{ fontFamily: "var(--mono)", fontSize: "0.9em", background: "rgba(167,139,250,0.15)", padding: "1px 5px", borderRadius: 4, color: "var(--accent)" }}>{t.content}</code>;
        if (t.type === "bold")       return <strong key={i} style={{ fontWeight: 700 }}>{t.content}</strong>;
        if (t.type === "italic")     return <em key={i}>{t.content}</em>;
        if (t.type === "bolditalic") return <strong key={i}><em>{t.content}</em></strong>;
        return <span key={i}>{t.content}</span>;
      })}
    </span>
  );
}

function renderMarkdown(text: string, isUser: boolean): React.ReactNode[] {
  const lines = text.split("\n");
  const out: React.ReactNode[] = [];
  const listItems: React.ReactNode[] = [];
  let listType: "ul" | "ol" | null = null;
  let idx = 0;

  function flushList() {
    if (!listItems.length) return;
    const Tag = listType === "ul" ? "ul" : "ol";
    out.push(<Tag key={`lst-${idx++}`} style={{ margin: "4px 0 4px 18px", paddingLeft: 4 }}>{[...listItems]}</Tag>);
    listItems.length = 0; listType = null;
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // headings
    const hm = line.match(/^(#{1,3}) (.+)/);
    if (hm) {
      flushList();
      const lvl = hm[1].length;
      const sz  = lvl === 1 ? 20 : lvl === 2 ? 16 : 14;
      out.push(
        <div key={idx++} style={{
          fontSize: sz, fontWeight: 700, margin: `${lvl === 1 ? 14 : 10}px 0 ${lvl === 1 ? 6 : 4}px`,
          borderBottom: lvl <= 2 ? "1px solid rgba(255,255,255,0.08)" : undefined,
          paddingBottom: lvl <= 2 ? 4 : undefined, letterSpacing: "-0.02em",
        }}>{renderInline(hm[2], idx)}</div>
      );
      continue;
    }

    // hr
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      flushList();
      out.push(<hr key={idx++} style={{ border: "none", borderTop: "1px solid rgba(255,255,255,0.1)", margin: "10px 0" }} />);
      continue;
    }

    // blockquote
    const bq = line.match(/^> ?(.*)/);
    if (bq) {
      flushList();
      out.push(
        <div key={idx++} style={{ borderLeft: "3px solid rgba(167,139,250,0.4)", paddingLeft: 10, margin: "3px 0", color: "var(--muted)", fontStyle: "italic" }}>
          {renderInline(bq[1], idx)}
        </div>
      );
      continue;
    }

    // unordered list
    const ul = line.match(/^[-*+] (.*)/);
    if (ul) {
      if (listType !== "ul") flushList();
      listType = "ul";
      listItems.push(<li key={idx++} style={{ marginBottom: 1 }}>{renderInline(ul[1], idx)}</li>);
      continue;
    }

    // ordered list
    const ol = line.match(/^\d+\. (.*)/);
    if (ol) {
      if (listType !== "ol") flushList();
      listType = "ol";
      listItems.push(<li key={idx++} style={{ marginBottom: 1 }}>{renderInline(ol[1], idx)}</li>);
      continue;
    }

    // blank line
    if (!line.trim()) {
      flushList();
      out.push(<div key={idx++} style={{ height: 6 }} />);
      continue;
    }

    // regular paragraph line
    flushList();
    out.push(<div key={idx++} style={{ margin: "1px 0", lineHeight: 1.6 }}>{renderInline(line, idx)}</div>);
  }

  flushList();
  return out;
}

function renderContent(content: string, isUser: boolean) {
  const parts = content.split(/```(?:[a-zA-Z0-9._-]*)?\n?/);
  return parts.map((part, i) => {
    if (!part) return null;
    if (i % 2 === 1) {
      return (
        <pre key={i} style={{
          display: "block", margin: "8px 0", padding: "10px 12px", borderRadius: 6,
          background: isUser ? "rgba(0,0,0,0.25)" : "var(--bg)",
          fontFamily: "var(--mono)", fontSize: 12, overflowX: "auto",
          border: "1px solid var(--border)", lineHeight: 1.5,
        }}>{part.trimEnd()}</pre>
      );
    }
    return isUser
      ? <span key={i} style={{ whiteSpace: "pre-wrap" }}>{part}</span>
      : <span key={i}>{renderMarkdown(part, isUser)}</span>;
  });
}

interface ModelOption { id: string; desc: string; vram: string; }

const MODEL_CATALOG: ModelOption[] = [
  { id: "devstral-small-2:24b", desc: "Mistral's purpose-built agentic coding model",    vram: "15 GB"  },
  { id: "qwen3.6:27b",          desc: "Top-tier coding & reasoning — Qwen 2026",         vram: "17 GB"  },
  { id: "qwen3:30b-a3b",        desc: "MoE architecture — 30B params, 3B active",        vram: "17 GB"  },
  { id: "qwen3:14b",            desc: "Excellent tool use & instruction following",       vram: "9.3 GB" },
  { id: "gemma4:12b",           desc: "Google's 2026 flagship — strong general tasks",   vram: "8 GB"   },
  { id: "qwen3:8b",             desc: "Fast & capable — best small agentic model",       vram: "5.2 GB" },
];

function ModelItem({ m, isLive, onSelect, active }: {
  m: ModelOption; isLive: boolean; onSelect: () => void; active: boolean;
}) {
  return (
    <div onClick={onSelect}
      style={{
        display: "flex", alignItems: "center", gap: 12, padding: "9px 14px",
        cursor: "pointer",
        background: active ? "rgba(167,139,250,0.1)" : "transparent",
        transition: "background 0.1s",
      }}
      onMouseEnter={e => { if (!active) (e.currentTarget as HTMLDivElement).style.background = "rgba(255,255,255,0.04)"; }}
      onMouseLeave={e => { if (!active) (e.currentTarget as HTMLDivElement).style.background = "transparent"; }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontFamily: "var(--mono)", fontSize: 12, color: active ? "var(--accent)" : "var(--text)", whiteSpace: "nowrap" }}>{m.id}</div>
        <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 2 }}>{m.desc}</div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 3, flexShrink: 0 }}>
        {isLive && (
          <span style={{ fontSize: 9, background: "rgba(52,211,153,0.15)", color: "var(--green)",
            border: "1px solid rgba(52,211,153,0.3)", borderRadius: 4, padding: "1px 5px", fontWeight: 700, letterSpacing: "0.05em" }}>
            LIVE
          </span>
        )}
        <span style={{ fontSize: 10, color: "var(--muted)", fontFamily: "var(--mono)" }}>{m.vram}</span>
      </div>
    </div>
  );
}

function ModelPicker({ model, setModel, liveModels }: {
  model: string; setModel: (m: string) => void; liveModels: string[];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  const isLive = liveModels.includes(model);
  const live    = MODEL_CATALOG.filter(m => liveModels.includes(m.id));
  const offline = MODEL_CATALOG.filter(m => !liveModels.includes(m.id));

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button onClick={() => setOpen(o => !o)} style={{
        display: "flex", alignItems: "center", gap: 8,
        background: open ? "rgba(167,139,250,0.12)" : "rgba(255,255,255,0.05)",
        border: `1px solid ${open ? "rgba(167,139,250,0.4)" : "rgba(255,255,255,0.1)"}`,
        borderRadius: 9, padding: "5px 10px 5px 12px", cursor: "pointer",
        transition: "background 0.15s, border-color 0.15s",
      }}>
        <span style={{ fontFamily: "var(--mono)", fontSize: 12, color: model ? "var(--accent)" : "var(--muted)" }}>
          {model || "select model"}
        </span>
        {isLive && (
          <span style={{ fontSize: 9, background: "rgba(52,211,153,0.15)", color: "var(--green)",
            border: "1px solid rgba(52,211,153,0.3)", borderRadius: 4, padding: "1px 5px", fontWeight: 700 }}>
            LIVE
          </span>
        )}
        <span style={{ color: "var(--muted)", fontSize: 9, marginLeft: 2 }}>{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div style={{
          position: "absolute", top: "calc(100% + 6px)", left: 0, zIndex: 200,
          background: "#0d0f18",
          border: "1px solid rgba(167,139,250,0.22)",
          borderRadius: 12, overflow: "hidden",
          boxShadow: "0 20px 48px rgba(0,0,0,0.7), 0 0 0 1px rgba(167,139,250,0.08)",
          minWidth: 340,
        }}>
          {live.length > 0 && (
            <>
              <div style={{ padding: "8px 14px 4px", fontSize: 9, color: "var(--green)",
                textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 700, opacity: 0.8 }}>
                ● Live agents
              </div>
              {live.map(m => (
                <ModelItem key={m.id} m={m} isLive onSelect={() => { setModel(m.id); setOpen(false); }} active={model === m.id} />
              ))}
              {offline.length > 0 && <div style={{ height: 1, background: "rgba(255,255,255,0.07)", margin: "4px 0" }} />}
            </>
          )}
          {offline.length > 0 && (
            <>
              <div style={{ padding: "8px 14px 4px", fontSize: 9, color: "var(--muted)",
                textTransform: "uppercase", letterSpacing: "0.1em", fontWeight: 700 }}>
                On-demand
              </div>
              {offline.map(m => (
                <ModelItem key={m.id} m={m} isLive={false} onSelect={() => { setModel(m.id); setOpen(false); }} active={model === m.id} />
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Chat({ apiKey }: { apiKey: string }) {
  const [messages, setMessages] = useLocalStorage<ChatMessage[]>("swarm_chat_messages", []);
  const [input, setInput]       = useState("");
  const [model, setModel]       = useLocalStorage<string>("swarm_chat_model", "");
  const [liveModels, setLiveModels] = useState<string[]>([]);
  const [customModel, setCustomModel] = useState("");
  const [showCustom, setShowCustom]   = useState(false);
  const [busy, setBusy]         = useState(false);
  const [error, setError]       = useState("");
  const [elapsed, setElapsed]   = useState(0);
  const [chatPhase, setChatPhase] = useState<ChatPhase>("preparing");
  const chatRequest = useRef<AbortController | null>(null);
  const busySince               = useRef<number>(0);
  const bottomRef   = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const loadModels = useCallback(() => {
    fetch("/swarm/api/v1/models", { headers: { "X-API-Key": apiKey } })
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        const ids: string[] = data.data?.map((m: { id: string }) => m.id) ?? [];
        setLiveModels(ids);
        setModel(cur => {
          if (cur) return cur;
          return ids[0] ?? MODEL_CATALOG[0].id;
        });
      })
      .catch(() => setLiveModels([]));
  }, [apiKey]);

  useEffect(() => { loadModels(); }, [loadModels]);

  useEffect(() => () => {
    chatRequest.current?.abort();
    chatRequest.current = null;
  }, []);

  const activeModel = showCustom ? customModel.trim() : model;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  useEffect(() => {
    if (!busy) { setElapsed(0); return; }
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - busySince.current) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [busy]);

  async function send() {
    const text = input.trim();
    if (!text || busy || chatRequest.current) return;
    if (!activeModel) { setError("Select or enter a model first."); return; }

    const newMsgs: ChatMessage[] = [...messages, { role: "user", content: text }];
    setMessages(newMsgs);
    setInput("");
    setBusy(true);
    setElapsed(0);
    busySince.current = Date.now();
    setError("");
    setChatPhase("preparing");
    const controller = new AbortController();
    chatRequest.current = controller;

    try {
      const data = await requestChatCompletion(apiKey, { model: activeModel, messages: newMsgs }, {
        signal: controller.signal,
        onPhase: setChatPhase,
      });
      if (controller.signal.aborted) return;
      const reply: string = data.choices?.[0]?.message?.content ?? "(empty response)";
      setMessages([...newMsgs, { role: "assistant", content: reply }]);
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (chatRequest.current === controller) {
        chatRequest.current = null;
        setBusy(false);
        setTimeout(() => textareaRef.current?.focus(), 0);
      }
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <div style={{
      display: "flex", flexDirection: "column",
      height: "calc(100vh - 300px)", minHeight: 440,
      background: "rgba(255,255,255,0.025)",
      backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)",
      borderRadius: 16, border: "1px solid rgba(255,255,255,0.08)", overflow: "hidden",
    }}>
      {/* toolbar */}
      <div style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "10px 16px", borderBottom: "1px solid rgba(255,255,255,0.07)",
        background: "rgba(255,255,255,0.02)", flexShrink: 0,
      }}>
        <span style={{ color: "var(--muted)", fontSize: 10, textTransform: "uppercase",
          letterSpacing: "0.09em", fontWeight: 700 }}>Model</span>

        {!showCustom ? (
          <>
            <ModelPicker model={model} setModel={setModel} liveModels={liveModels} />
            {liveModels.length === 0 && (
              <span style={{ color: "var(--yellow)", fontSize: 11 }}>no live models</span>
            )}
          </>
        ) : (
          <input
            autoFocus
            value={customModel}
            onChange={e => setCustomModel(e.target.value)}
            placeholder="e.g. qwen3:8b"
            style={{
              background: "rgba(167,139,250,0.1)", color: "var(--text)",
              border: "1px solid rgba(167,139,250,0.35)", borderRadius: 8,
              padding: "4px 10px", fontSize: 12, fontFamily: "var(--mono)",
              outline: "none", width: 200,
            }}
          />
        )}

        <button
          onClick={() => setShowCustom(s => !s)}
          title={showCustom ? "Pick from list" : "Type a custom model"}
          style={{
            background: showCustom ? "rgba(167,139,250,0.18)" : "none",
            color: showCustom ? "var(--accent)" : "var(--muted)",
            border: `1px solid ${showCustom ? "rgba(167,139,250,0.35)" : "rgba(255,255,255,0.1)"}`,
            borderRadius: 7, fontSize: 11, padding: "3px 9px", cursor: "pointer",
          }}
        >{showCustom ? "list" : "custom"}</button>

        <button onClick={loadModels} title="Refresh live model list" style={{
          background: "none", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 7,
          color: "var(--muted)", fontSize: 13, padding: "2px 8px", cursor: "pointer",
        }}>↻</button>
        {messages.length > 0 && (
          <button disabled={busy} onClick={() => { setMessages([]); setError(""); }} style={{
            marginLeft: "auto", background: "none", border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: 7, color: "var(--muted)", fontSize: 11,
            padding: "3px 10px", cursor: "pointer",
          }}>clear</button>
        )}
      </div>

      {/* message list */}
      <div style={{ flex: 1, overflowY: "auto", padding: "20px" }}>
        {messages.length === 0 && !busy && (
          <div style={{ textAlign: "center", color: "var(--muted)", marginTop: 60 }}>
            <div style={{ fontSize: 36, marginBottom: 10 }}>⌨</div>
            <div style={{ fontSize: 15, fontWeight: 500 }}>Ask the swarm anything</div>
            {model
              ? <div style={{ fontSize: 12, marginTop: 6 }}>
                  Running on{" "}
                  <code style={{ fontFamily: "var(--mono)", color: "var(--accent)" }}>{model}</code>
                </div>
              : <div style={{ fontSize: 12, marginTop: 6, color: "var(--yellow)" }}>
                  Distributed requests can wait for GPU capacity
                </div>
            }
          </div>
        )}

        {messages.map((msg, i) => {
          const isUser = msg.role === "user";
          return (
            <div key={i} style={{
              display: "flex",
              justifyContent: isUser ? "flex-end" : "flex-start",
              alignItems: "flex-end",
              gap: 8,
              marginBottom: 16,
            }}>
              {!isUser && (
                <div style={{
                  width: 28, height: 28, borderRadius: "50%", flexShrink: 0,
                  background: "linear-gradient(135deg, #a78bfa 0%, #22d3ee 100%)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  fontSize: 9, fontWeight: 800, color: "#fff",
                  boxShadow: "0 0 12px rgba(167,139,250,0.4)",
                }}>AI</div>
              )}
              <div style={{
                maxWidth: "72%",
                background: isUser
                  ? "linear-gradient(135deg, #7c3aed 0%, #6366f1 100%)"
                  : "rgba(255,255,255,0.04)",
                color: "var(--text)",
                borderRadius: isUser ? "18px 18px 4px 18px" : "18px 18px 18px 4px",
                padding: "11px 15px", fontSize: 14, lineHeight: 1.65,
                border: isUser ? "none" : "1px solid rgba(255,255,255,0.08)",
                backdropFilter: isUser ? undefined : "blur(8px)",
                boxShadow: isUser ? "0 4px 16px rgba(124,58,237,0.3)" : undefined,
              }}>
                {renderContent(msg.content, isUser)}
              </div>
            </div>
          );
        })}

        {/* typing indicator */}
        {busy && (
          <div style={{ display: "flex", alignItems: "flex-end", gap: 8, marginBottom: 16 }}>
            <div style={{
              width: 28, height: 28, borderRadius: "50%", flexShrink: 0,
              background: "linear-gradient(135deg, #a78bfa 0%, #22d3ee 100%)",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 9, fontWeight: 800, color: "#fff",
              boxShadow: "0 0 12px rgba(167,139,250,0.4)",
            }}>AI</div>
            <div style={{
              background: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.08)",
              borderRadius: "18px 18px 18px 4px", padding: "13px 17px",
              display: "flex", gap: 5, alignItems: "center",
            }}>
              {[0, 0.28, 0.56].map(delay => (
                <span key={delay} style={{
                  width: 7, height: 7, borderRadius: "50%",
                  background: "var(--accent)", display: "inline-block",
                  animation: `bounce 1.4s ${delay}s cubic-bezier(0.45, 0, 0.55, 1) infinite`,
                }} />
              ))}
              <span role="status" aria-live="polite" style={{ marginLeft: 8, fontSize: 12, color: "var(--muted)" }}>
                {chatPhase === "waiting_for_capacity"
                  ? "Waiting for available GPUs — agents may still be reconnecting (up to 30 minutes)…"
                  : "Waiting for model setup or response…"}
              </span>
              {elapsed >= 5 && (
                <span style={{
                  marginLeft: 8, fontSize: 11, color: "var(--muted)",
                  fontFamily: "var(--mono)", opacity: 0.8,
                }}>{elapsed}s</span>
              )}
            </div>
          </div>
        )}

        {error && (
          <div style={{
            color: "var(--red)", fontSize: 12, padding: "8px 12px", marginBottom: 12,
            background: "rgba(239,68,68,0.1)", borderRadius: 6,
            border: "1px solid rgba(239,68,68,0.3)",
          }}>{error}</div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* input row */}
      <div style={{
        padding: "12px 16px", borderTop: "1px solid rgba(255,255,255,0.07)",
        display: "flex", gap: 10, flexShrink: 0,
        background: "rgba(255,255,255,0.02)",
      }}>
        <textarea
          ref={textareaRef}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          onInput={e => {
            const t = e.currentTarget;
            t.style.height = "auto";
            t.style.height = Math.min(t.scrollHeight, 120) + "px";
          }}
          placeholder="Message the swarm… (Enter to send, Shift+Enter for newline)"
          rows={1}
          style={{
            flex: 1, background: "rgba(255,255,255,0.05)",
            border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: 10, color: "var(--text)", fontSize: 14,
            padding: "10px 14px", fontFamily: "inherit", resize: "none",
            outline: "none", lineHeight: 1.5, overflowY: "auto",
            transition: "border-color 0.15s",
          }}
        />
        <button
          hidden={!busy}
          onClick={() => chatRequest.current?.abort()}
          style={{ background: "none", color: "var(--muted)", border: "1px solid rgba(255,255,255,0.15)",
            borderRadius: 10, padding: "0 14px", cursor: "pointer" }}
        >Cancel</button>
        <button
          onClick={send}
          disabled={busy || !input.trim() || !model}
          style={{
            background: busy || !input.trim() || !model
              ? "rgba(255,255,255,0.06)"
              : "linear-gradient(135deg, #7c3aed 0%, #6366f1 100%)",
            color: busy || !input.trim() || !model ? "var(--muted)" : "#fff",
            border: "none", borderRadius: 10, padding: "0 22px",
            fontSize: 14, fontWeight: 600, flexShrink: 0,
            cursor: busy || !input.trim() || !model ? "not-allowed" : "pointer",
            transition: "opacity 0.15s",
            boxShadow: busy || !input.trim() || !model ? "none" : "0 4px 14px rgba(124,58,237,0.35)",
          }}
        >Send</button>
      </div>
    </div>
  );
}

// ── usage analytics ───────────────────────────────────────────────────────────

// GPT-4o mini reference pricing for cost-equivalent estimate
const COST_INPUT  = 0.15 / 1_000_000;   // $0.15 / 1M prompt tokens
const COST_OUTPUT = 0.60 / 1_000_000;   // $0.60 / 1M completion tokens

const CH = {
  prompt:     "#a78bfa",
  completion: "#34d399",
  cost:       "#fbbf24",
  latency:    "#22d3ee",
  error:      "#f87171",
  grid:       "rgba(255,255,255,0.05)",
  axis:       "#50607a",
};

const ttStyle = {
  contentStyle: { background: "rgba(13,15,24,0.92)", backdropFilter: "blur(12px)", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 10, fontSize: 12 },
  labelStyle:   { color: "#50607a", fontSize: 11 },
  itemStyle:    { color: "#dce4f0" },
};

type RangePreset  = "24h" | "7d" | "30d" | "90d";
type BucketMode   = "auto" | "hour" | "day" | "week";

const REASON_COLORS: Record<string, string> = {
  stop:       "#34d399",
  tool_calls: "#a78bfa",
  length:     "#fbbf24",
  error:      "#f87171",
  timeout:    "#f87171",
  unknown:    "#50607a",
};

function fmtBucket(b: string): string {
  if (b.startsWith("20") && b.includes("W")) return b.slice(5); // "2026-W38" → "W38"
  if (b.includes("T")) {
    const d = new Date(b.replace("T", " ") + "Z");
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  const parts = b.split("-");
  if (parts.length === 3) return `${parts[1]}/${parts[2]}`;
  return b;
}

function fmtNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000)     return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{
      background: "rgba(255,255,255,0.025)",
      backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)",
      border: "1px solid rgba(255,255,255,0.08)",
      borderTop: "1px solid rgba(167,139,250,0.2)",
      borderRadius: 14, padding: "18px 20px",
    }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: "var(--muted)",
        textTransform: "uppercase", letterSpacing: "0.1em", marginBottom: 16 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function UsageView({ apiKey }: { apiKey: string }) {
  const [range, setRange]           = useState<RangePreset>("7d");
  const [bucketMode, setBucketMode] = useState<BucketMode>("auto");
  const [fromDate, setFromDate]     = useState("");
  const [toDate, setToDate]         = useState("");
  const [useCustom, setUseCustom]   = useState(false);
  const [data, setData]             = useState<UsageStats | null>(null);
  const [loading, setLoading]       = useState(false);
  const [err, setErr]               = useState("");

  const fetchUsage = useCallback(async () => {
    setLoading(true);
    setErr("");
    const now = Date.now() / 1000;
    let fromTs: number, toTs = now;

    if (useCustom && fromDate && toDate) {
      fromTs = new Date(fromDate).getTime() / 1000;
      toTs   = new Date(toDate).getTime()   / 1000 + 86399;
    } else {
      const secs: Record<RangePreset, number> = { "24h": 86400, "7d": 7*86400, "30d": 30*86400, "90d": 90*86400 };
      fromTs = now - secs[range];
    }

    let bucket: string;
    if (bucketMode === "auto") {
      const span = toTs - fromTs;
      bucket = span <= 2 * 86400 ? "hour" : span >= 60 * 86400 ? "week" : "day";
    } else {
      bucket = bucketMode;
    }

    try {
      const res = await apiFetch(`/stats/usage?from_ts=${fromTs}&to_ts=${toTs}&bucket=${bucket}`, apiKey);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setData(await res.json() as UsageStats);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [range, bucketMode, fromDate, toDate, useCustom, apiKey]);

  useEffect(() => { fetchUsage(); }, [fetchUsage]);

  const series = (data?.series ?? []).map(s => ({
    ...s,
    cost_usd:     s.prompt_tokens * COST_INPUT + s.completion_tokens * COST_OUTPUT,
    success_rate: s.requests > 0 ? Math.round(100 * (s.requests - s.errors) / s.requests) : 100,
    label:        fmtBucket(s.bucket),
  }));

  const totalTokens    = series.reduce((a, s) => a + s.total_tokens, 0);
  const totalCost      = series.reduce((a, s) => a + s.cost_usd,     0);
  const totalRequests  = series.reduce((a, s) => a + s.requests,     0);
  const totalErrors    = series.reduce((a, s) => a + s.errors,       0);
  const overallSucc    = totalRequests > 0 ? Math.round(100 * (totalRequests - totalErrors) / totalRequests) : 100;
  const avgLatency     = (() => {
    const valid = (data?.by_model ?? []).filter(m => m.avg_ms != null);
    return valid.length ? valid.reduce((a, m) => a + m.avg_ms!, 0) / valid.length : null;
  })();
  const avgQueueWait   = (() => {
    const valid = (data?.by_model ?? []).filter(m => m.avg_queue_ms != null);
    return valid.length ? valid.reduce((a, m) => a + m.avg_queue_ms!, 0) / valid.length : null;
  })();

  const byModel    = (data?.by_model    ?? []).map(m => ({ ...m, model: m.model.length > 22 ? m.model.slice(0, 22) + "…" : m.model }));
  const byUser     = (data?.by_user     ?? []).map(u => ({ ...u, label: u.user_id.length > 20 ? u.user_id.slice(0, 20) + "…" : u.user_id }));
  const byHostname = (data?.by_hostname ?? []).map(h => ({ ...h, label: h.hostname.length > 18 ? h.hostname.slice(0, 18) + "…" : h.hostname }));
  const byReason   = data?.by_finish_reason ?? [];
  const byHour     = data?.by_hour ?? [];

  const RANGES:  RangePreset[] = ["24h", "7d", "30d", "90d"];
  const BUCKETS: BucketMode[]  = ["auto", "hour", "day", "week"];
  const btnBase: React.CSSProperties = {
    padding: "4px 14px", borderRadius: 5, fontSize: 12, fontWeight: 600,
    border: "1px solid var(--border)", cursor: "pointer",
    transition: "background 0.12s, color 0.12s",
  };
  const active = (on: boolean) => ({
    background: on ? "var(--accent)" : "transparent",
    color:      on ? "#0f172a"       : "var(--muted)",
  });

  return (
    <div>
      {/* range + bucket controls */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
        <span style={{ color: "var(--muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>Range</span>
        {RANGES.map(r => (
          <button key={r} onClick={() => { setRange(r); setUseCustom(false); }}
            style={{ ...btnBase, ...active(!useCustom && range === r) }}>{r}</button>
        ))}
        <span style={{ color: "var(--border)" }}>|</span>
        <input type="date" value={fromDate} onChange={e => setFromDate(e.target.value)}
          style={{ background: "var(--surface)", border: "1px solid var(--border)",
            color: "var(--text)", borderRadius: 5, padding: "4px 8px", fontSize: 12,
            outline: "none", colorScheme: "dark" }} />
        <span style={{ color: "var(--muted)", fontSize: 12 }}>→</span>
        <input type="date" value={toDate} onChange={e => setToDate(e.target.value)}
          style={{ background: "var(--surface)", border: "1px solid var(--border)",
            color: "var(--text)", borderRadius: 5, padding: "4px 8px", fontSize: 12,
            outline: "none", colorScheme: "dark" }} />
        <button onClick={() => setUseCustom(true)} style={{ ...btnBase, ...active(useCustom) }}>Apply</button>
        <button onClick={fetchUsage} title="Refresh" style={{ ...btnBase, marginLeft: "auto" }}>↻</button>
        {loading && <span style={{ color: "var(--muted)", fontSize: 12 }}>Loading…</span>}
        {err     && <span style={{ color: "var(--red)",   fontSize: 12 }}>⚠ {err}</span>}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 20, flexWrap: "wrap" }}>
        <span style={{ color: "var(--muted)", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em" }}>Bucket</span>
        {BUCKETS.map(b => (
          <button key={b} onClick={() => setBucketMode(b)}
            style={{ ...btnBase, ...active(bucketMode === b), textTransform: "capitalize" }}>{b}</button>
        ))}
        {data && <span style={{ color: "var(--muted)", fontSize: 11, marginLeft: 8 }}>
          — using <code style={{ fontFamily: "var(--mono)", color: "var(--accent)" }}>{data.bucket}</code>
        </span>}
      </div>

      {/* KPI cards */}
      <div style={{ display: "flex", gap: 12, marginBottom: 20, flexWrap: "wrap" }}>
        {[
          { label: "Total tokens",       value: fmtNum(totalTokens),        color: "var(--accent)"  },
          { label: "Prompt tokens",      value: fmtNum(data?.by_model.reduce((a,m) => a + m.prompt_tokens, 0) ?? 0),     color: CH.prompt     },
          { label: "Completion tokens",  value: fmtNum(data?.by_model.reduce((a,m) => a + m.completion_tokens, 0) ?? 0), color: CH.completion },
          { label: "Est. cost (GPT-4o mini ref.)", value: `$${totalCost.toFixed(4)}`, color: CH.cost },
          { label: "Requests",           value: totalRequests,              color: "var(--text)"    },
          { label: "Success rate",       value: `${overallSucc}%`,          color: overallSucc < 90 ? "var(--red)" : "var(--green)" },
          { label: "Avg inference",      value: avgLatency    != null ? `${avgLatency.toFixed(0)} ms`   : "—", color: CH.latency },
          { label: "Avg queue wait",     value: avgQueueWait  != null ? `${avgQueueWait.toFixed(0)} ms` : "—", color: CH.cost    },
        ].map(c => (
          <div key={c.label} style={{
            background: "var(--surface)", border: "1px solid var(--border)",
            borderRadius: 10, padding: "14px 22px", flex: "1 1 130px",
          }}>
            <div style={{ fontSize: 26, fontWeight: 700, color: c.color, lineHeight: 1 }}>{c.value}</div>
            <div style={{ fontSize: 10, color: "var(--muted)", textTransform: "uppercase",
              letterSpacing: "0.06em", marginTop: 4 }}>{c.label}</div>
          </div>
        ))}
      </div>

      {/* empty state */}
      {!loading && series.length === 0 && (
        <div style={{ textAlign: "center", color: "var(--muted)", padding: "60px 0", fontSize: 14 }}>
          No usage data in this range yet — usage is logged automatically for every chat completion.
        </div>
      )}

      {series.length > 0 && (
        <>
          {/* row 1: tokens + cost */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
            <ChartCard title="Tokens over time — prompt + completion">
              <ResponsiveContainer width="100%" height={220}>
                <AreaChart data={series} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="gPrompt" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor={CH.prompt}     stopOpacity={0.35} />
                      <stop offset="95%" stopColor={CH.prompt}     stopOpacity={0.03} />
                    </linearGradient>
                    <linearGradient id="gCompletion" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor={CH.completion} stopOpacity={0.35} />
                      <stop offset="95%" stopColor={CH.completion} stopOpacity={0.03} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} />
                  <XAxis dataKey="label" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis tickFormatter={fmtNum} tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} width={44} />
                  <Tooltip {...ttStyle} formatter={v => [fmtNum(Number(v)), ""]} />
                  <Legend wrapperStyle={{ fontSize: 11, color: "var(--muted)" }} />
                  <Area type="monotone" dataKey="prompt_tokens"     name="Prompt"     stackId="1"
                    stroke={CH.prompt}     fill="url(#gPrompt)"     strokeWidth={2} dot={false} />
                  <Area type="monotone" dataKey="completion_tokens" name="Completion" stackId="1"
                    stroke={CH.completion} fill="url(#gCompletion)" strokeWidth={2} dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </ChartCard>

            <ChartCard title="Estimated cost — GPT-4o mini pricing reference">
              <div style={{ fontSize: 10, color: "var(--muted)", marginTop: -10, marginBottom: 10 }}>
                $0.15/1M prompt · $0.60/1M completion — what this traffic would cost on an external API
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={series} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} />
                  <XAxis dataKey="label" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis tickFormatter={v => `$${v.toFixed(4)}`} tick={{ fill: CH.axis, fontSize: 10 }} axisLine={false} tickLine={false} width={60} />
                  <Tooltip {...ttStyle} formatter={v => [`$${Number(v).toFixed(5)}`, "Est. cost"]} />
                  <Line type="monotone" dataKey="cost_usd" name="Est. cost"
                    stroke={CH.cost} strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartCard>
          </div>

          {/* row 2: requests + response time by model */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
            <ChartCard title="Requests by model">
              <ResponsiveContainer width="100%" height={Math.max(160, byModel.length * 38)}>
                <BarChart data={byModel} layout="vertical" margin={{ top: 0, right: 16, left: 4, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} horizontal={false} />
                  <XAxis type="number" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis type="category" dataKey="model" tick={{ fill: "var(--text)", fontSize: 11 }} axisLine={false} tickLine={false} width={140} />
                  <Tooltip {...ttStyle} />
                  <Bar dataKey="requests" name="Requests" fill={CH.prompt} radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartCard>

            <ChartCard title="Avg response time by model (ms)">
              <ResponsiveContainer width="100%" height={Math.max(160, byModel.length * 38)}>
                <BarChart data={byModel.filter(m => m.avg_ms != null)} layout="vertical"
                  margin={{ top: 0, right: 16, left: 4, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} horizontal={false} />
                  <XAxis type="number" tickFormatter={v => `${v.toFixed(0)}`} tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis type="category" dataKey="model" tick={{ fill: "var(--text)", fontSize: 11 }} axisLine={false} tickLine={false} width={140} />
                  <Tooltip {...ttStyle} formatter={v => [`${Number(v).toFixed(0)} ms`, "Avg latency"]} />
                  <Bar dataKey="avg_ms" name="Avg latency (ms)" fill={CH.latency} radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartCard>
          </div>

          {/* row 3: tokens by model + top users */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <ChartCard title="Tokens by model — prompt + completion">
              <ResponsiveContainer width="100%" height={Math.max(160, byModel.length * 38)}>
                <BarChart data={byModel} layout="vertical" margin={{ top: 0, right: 16, left: 4, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} horizontal={false} />
                  <XAxis type="number" tickFormatter={fmtNum} tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis type="category" dataKey="model" tick={{ fill: "var(--text)", fontSize: 11 }} axisLine={false} tickLine={false} width={140} />
                  <Tooltip {...ttStyle} formatter={v => [fmtNum(Number(v)), ""]} />
                  <Legend wrapperStyle={{ fontSize: 11, color: "var(--muted)" }} />
                  <Bar dataKey="prompt_tokens"     name="Prompt"     stackId="t" fill={CH.prompt}     radius={[0, 0, 0, 0]} />
                  <Bar dataKey="completion_tokens" name="Completion" stackId="t" fill={CH.completion} radius={[0, 4, 4, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartCard>

            <ChartCard title="Top users by request count">
              {byUser.length === 0 ? (
                <div style={{ color: "var(--muted)", fontSize: 12, padding: "24px 0" }}>No user data</div>
              ) : (
                <ResponsiveContainer width="100%" height={Math.max(160, byUser.length * 38)}>
                  <BarChart data={byUser} layout="vertical" margin={{ top: 0, right: 16, left: 4, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} horizontal={false} />
                    <XAxis type="number" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                    <YAxis type="category" dataKey="label" tick={{ fill: "var(--text)", fontSize: 11 }} axisLine={false} tickLine={false} width={130} />
                    <Tooltip {...ttStyle} />
                    <Bar dataKey="requests" name="Requests" fill={CH.cost} radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </ChartCard>
          </div>

          {/* row 4: success rate over time + requests/errors over time */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginTop: 16 }}>
            <ChartCard title="Success rate over time (%)">
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={series} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} />
                  <XAxis dataKey="label" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis domain={[0, 100]} tickFormatter={v => `${v}%`} tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} width={40} />
                  <Tooltip {...ttStyle} formatter={v => [`${Number(v).toFixed(1)}%`, "Success rate"]} />
                  <Line type="monotone" dataKey="success_rate" name="Success rate"
                    stroke={CH.completion} strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </ChartCard>

            <ChartCard title="Requests &amp; errors over time">
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={series} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} />
                  <XAxis dataKey="label" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} width={32} />
                  <Tooltip {...ttStyle} />
                  <Legend wrapperStyle={{ fontSize: 11, color: "var(--muted)" }} />
                  <Bar dataKey="requests" name="Requests" stackId="r" fill={CH.prompt}     radius={[0, 0, 0, 0]} />
                  <Bar dataKey="errors"   name="Errors"   stackId="r" fill={CH.error}      radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </ChartCard>
          </div>

          {/* row 5: inference vs queue wait over time */}
          {series.some(s => s.avg_queue_ms != null) && (
            <div style={{ marginTop: 16 }}>
              <ChartCard title="Avg inference time vs queue wait (ms) — what Ollama spent vs how long the request waited in queue">
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={series} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} />
                    <XAxis dataKey="label" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                    <YAxis tickFormatter={v => `${v}ms`} tick={{ fill: CH.axis, fontSize: 10 }} axisLine={false} tickLine={false} width={52} />
                    <Tooltip {...ttStyle} formatter={v => [`${Number(v).toFixed(0)} ms`, ""]} />
                    <Legend wrapperStyle={{ fontSize: 11, color: "var(--muted)" }} />
                    <Bar dataKey="avg_ms"       name="Inference"  stackId="t" fill={CH.latency} radius={[0, 0, 0, 0]} />
                    <Bar dataKey="avg_queue_ms" name="Queue wait" stackId="t" fill={CH.cost}    radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </ChartCard>
            </div>
          )}

          {/* row 6: activity heatmap + finish reason + by hostname */}
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 1fr", gap: 16, marginTop: 16 }}>
            <ChartCard title="Activity by hour of day — all time in selected range">
              <ResponsiveContainer width="100%" height={200}>
                <BarChart data={byHour} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} />
                  <XAxis dataKey="label" tick={{ fill: CH.axis, fontSize: 10 }} axisLine={false} tickLine={false}
                    interval={2} />
                  <YAxis tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} width={28} />
                  <Tooltip {...ttStyle} formatter={v => [`${v} req`, "Requests"]} />
                  <Bar dataKey="requests" name="Requests" fill={CH.prompt}
                    radius={[3, 3, 0, 0]}
                    label={false} />
                </BarChart>
              </ResponsiveContainer>
            </ChartCard>

            <ChartCard title="Finish reason">
              {byReason.length === 0
                ? <div style={{ color: "var(--muted)", fontSize: 12, padding: "24px 0" }}>No data</div>
                : <ResponsiveContainer width="100%" height={200}>
                    <BarChart data={byReason} layout="vertical" margin={{ top: 0, right: 16, left: 4, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} horizontal={false} />
                      <XAxis type="number" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                      <YAxis type="category" dataKey="reason" tick={{ fill: "var(--text)", fontSize: 11 }} axisLine={false} tickLine={false} width={70} />
                      <Tooltip {...ttStyle} />
                      <Bar dataKey="count" name="Count" radius={[0, 4, 4, 0]}>
                        {byReason.map(r => (
                          <rect key={r.reason} fill={REASON_COLORS[r.reason] ?? "#64748b"} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
              }
            </ChartCard>

            <ChartCard title="By machine">
              {byHostname.length === 0
                ? <div style={{ color: "var(--muted)", fontSize: 12, padding: "24px 0" }}>No data</div>
                : <ResponsiveContainer width="100%" height={Math.max(200, byHostname.length * 42)}>
                    <BarChart data={byHostname} layout="vertical" margin={{ top: 0, right: 16, left: 4, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} horizontal={false} />
                      <XAxis type="number" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                      <YAxis type="category" dataKey="label" tick={{ fill: "var(--text)", fontSize: 11 }} axisLine={false} tickLine={false} width={100} />
                      <Tooltip {...ttStyle} />
                      <Bar dataKey="requests" name="Requests" fill={CH.completion} radius={[0, 4, 4, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
              }
            </ChartCard>
          </div>

          {/* row 7: queue wait by model */}
          {byModel.some(m => m.avg_queue_ms != null) && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginTop: 16 }}>
              <ChartCard title="Queue wait by model (ms) — avg time waiting before agent picks up">
                <ResponsiveContainer width="100%" height={Math.max(160, byModel.length * 38)}>
                  <BarChart data={byModel.filter(m => m.avg_queue_ms != null)} layout="vertical"
                    margin={{ top: 0, right: 16, left: 4, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} horizontal={false} />
                    <XAxis type="number" tickFormatter={v => `${v}ms`} tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                    <YAxis type="category" dataKey="model" tick={{ fill: "var(--text)", fontSize: 11 }} axisLine={false} tickLine={false} width={140} />
                    <Tooltip {...ttStyle} formatter={v => [`${Number(v).toFixed(0)} ms`, "Avg queue wait"]} />
                    <Bar dataKey="avg_queue_ms" name="Avg queue wait" fill={CH.cost} radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </ChartCard>

              <ChartCard title="Error count by model">
                <ResponsiveContainer width="100%" height={Math.max(160, byModel.length * 38)}>
                  <BarChart data={byModel} layout="vertical" margin={{ top: 0, right: 16, left: 4, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={CH.grid} horizontal={false} />
                    <XAxis type="number" tick={{ fill: CH.axis, fontSize: 11 }} axisLine={false} tickLine={false} />
                    <YAxis type="category" dataKey="model" tick={{ fill: "var(--text)", fontSize: 11 }} axisLine={false} tickLine={false} width={140} />
                    <Tooltip {...ttStyle} />
                    <Bar dataKey="errors" name="Errors" fill={CH.error} radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </ChartCard>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ── media generation ──────────────────────────────────────────────────────────

function AuthImage({ src, apiKey, style }: { src: string; apiKey: string; style?: React.CSSProperties }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let alive = true;
    fetch(src, { headers: { "X-API-Key": apiKey } })
      .then(r => { if (!r.ok) throw new Error(); return r.blob(); })
      .then(b => { if (alive) setBlobUrl(URL.createObjectURL(b)); })
      .catch(() => { if (alive) setErr(true); });
    return () => { alive = false; };
  }, [src, apiKey]);
  if (err) return <div style={{ color: "var(--red)", fontSize: 12 }}>Failed to load</div>;
  if (!blobUrl) return <div style={{ color: "var(--muted)", fontSize: 12, padding: "20px 0", textAlign: "center" }}>Loading…</div>;
  return <img src={blobUrl} style={style} />;
}

function AuthVideo({ src, apiKey, style }: { src: string; apiKey: string; style?: React.CSSProperties }) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let alive = true;
    fetch(src, { headers: { "X-API-Key": apiKey } })
      .then(r => { if (!r.ok) throw new Error(); return r.blob(); })
      .then(b => { if (alive) setBlobUrl(URL.createObjectURL(b)); })
      .catch(() => { if (alive) setErr(true); });
    return () => { alive = false; };
  }, [src, apiKey]);
  if (err) return <div style={{ color: "var(--red)", fontSize: 12 }}>Failed to load video</div>;
  if (!blobUrl) return <div style={{ color: "var(--muted)", fontSize: 12, padding: "20px 0", textAlign: "center" }}>Downloading video…</div>;
  return <video src={blobUrl} controls style={style} />;
}

function AuthVideoDownload({ src, apiKey, jobId }: { src: string; apiKey: string; jobId: string }) {
  async function download() {
    const res = await fetch(src, { headers: { "X-API-Key": apiKey } });
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `video-${jobId.slice(0, 8)}.mp4`;
    a.click(); URL.revokeObjectURL(url);
  }
  return (
    <button onClick={download} style={{
      marginTop: 8, background: "none", border: "1px solid rgba(255,255,255,0.12)",
      borderRadius: 7, color: "var(--muted)", fontSize: 11,
      padding: "4px 12px", cursor: "pointer",
    }}>⬇ download</button>
  );
}

const IMAGE_MODEL_CATALOG: MediaModel[] = [
  { id: "flux-schnell",  name: "FLUX.1-schnell",      vram_gb: 8,  hf_repo: "black-forest-labs/FLUX.1-schnell" },
  { id: "flux-dev",      name: "FLUX.1-dev",          vram_gb: 12, hf_repo: "black-forest-labs/FLUX.1-dev" },
  { id: "sdxl",          name: "Stable Diffusion XL", vram_gb: 8,  hf_repo: "stabilityai/stable-diffusion-xl-base-1.0" },
  { id: "sd3.5-medium",  name: "SD 3.5 Medium",       vram_gb: 8,  hf_repo: "stabilityai/stable-diffusion-3.5-medium" },
];

const VIDEO_MODEL_CATALOG: MediaModel[] = [
  { id: "ltx-video",        name: "LTX-Video",         vram_gb: 8,  hf_repo: "Lightricks/LTX-Video" },
  { id: "cogvideox-2b",     name: "CogVideoX-2B",      vram_gb: 8,  hf_repo: "THUDM/CogVideoX-2b" },
  { id: "cogvideox-5b",     name: "CogVideoX-5B",      vram_gb: 16, hf_repo: "THUDM/CogVideoX-5b" },
  { id: "wan-2.1-t2v-1.3b", name: "Wan 2.1 T2V 1.3B", vram_gb: 8,  hf_repo: "Wan-AI/Wan2.1-T2V-1.3B-Diffusers" },
];

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "9px 12px", boxSizing: "border-box",
  background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)",
  borderRadius: 8, color: "var(--text)", fontSize: 13, fontFamily: "inherit",
  outline: "none",
};

const labelStyle: React.CSSProperties = {
  fontSize: 10, color: "var(--muted)", textTransform: "uppercase",
  letterSpacing: "0.09em", fontWeight: 700, marginBottom: 5, display: "block",
};

function MediaModelPicker({ value, onChange, catalog }: {
  value: string; onChange: (v: string) => void; catalog: MediaModel[];
}) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
      {catalog.map(m => {
        const active = value === m.id;
        return (
          <button key={m.id} onClick={() => onChange(m.id)} style={{
            padding: "5px 10px", borderRadius: 7, fontSize: 11, cursor: "pointer",
            background: active ? "rgba(167,139,250,0.18)" : "rgba(255,255,255,0.04)",
            border: `1px solid ${active ? "rgba(167,139,250,0.45)" : "rgba(255,255,255,0.08)"}`,
            color: active ? "var(--accent)" : "var(--muted)",
            transition: "all 0.12s",
          }}>
            <div style={{ fontFamily: "var(--mono)", fontSize: 11 }}>{m.id}</div>
            <div style={{ fontSize: 9, marginTop: 2, color: active ? "var(--accent)" : "var(--muted)", opacity: 0.7 }}>
              {m.vram_gb} GB  ·  {m.name}
            </div>
          </button>
        );
      })}
    </div>
  );
}

function ImageGen({ apiKey }: { apiKey: string }) {
  const [model, setModel]     = useState("flux-schnell");
  const [prompt, setPrompt]   = useState("");
  const [neg, setNeg]         = useState("");
  const [size, setSize]       = useState("1024x1024");
  const [busy, setBusy]       = useState(false);
  const [error, setError]     = useState("");
  const [jobs, setJobs]       = useLocalStorage<ImageGenJob[]>("swarm_image_jobs", []);
  const [catalog, setCatalog] = useState<MediaModel[]>([]);

  useEffect(() => {
    fetch("/swarm/api/v1/images/models", { headers: { "X-API-Key": apiKey } })
      .then(r => r.json()).then(d => setCatalog(d.data ?? [])).catch(() => {});
  }, [apiKey]);

  // Orphaned in-flight jobs from a previous session can never complete.
  useEffect(() => {
    setJobs(prev => prev.map(j =>
      (j.status === "queued" || j.status === "processing")
        ? { ...j, status: "failed" as const, error: "Session ended — resubmit to regenerate" }
        : j
    ));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const caps = catalog.find(m => m.id === model);
  const sizes = caps?.sizes ?? ["1024x1024", "1280x720", "720x1280", "896x1152", "1152x896"];

  function handleModelChange(id: string) {
    setModel(id);
    const c = catalog.find(m => m.id === id);
    if (c?.sizes?.[0]) setSize(c.sizes[0]);
  }

  // Image API is synchronous (OpenAI-compatible): POST blocks until the image is ready.
  async function generate() {
    if (!prompt.trim() || busy) return;
    setBusy(true); setError("");
    const tempId = crypto.randomUUID();
    setJobs(prev => [
      { id: tempId, status: "processing" as const, created: Date.now() / 1000, model } as ImageGenJob,
      ...prev,
    ]);
    try {
      const res = await fetch("/swarm/api/v1/images/generations", {
        method: "POST",
        headers: { "X-API-Key": apiKey, "Content-Type": "application/json" },
        body: JSON.stringify({ model, prompt: prompt.trim(), negative_prompt: neg, n: 1, size }),
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({})) as { detail?: string };
        throw new Error(e.detail ?? `HTTP ${res.status}`);
      }
      const data = await res.json() as { created: number; data: Array<{ url: string }> };
      setJobs(prev => prev.map(j =>
        j.id === tempId ? { ...j, status: "completed" as const, data: data.data } : j
      ));
    } catch (e) {
      setJobs(prev => prev.map(j =>
        j.id === tempId ? { ...j, status: "failed" as const, error: String(e) } : j
      ));
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14, color: "var(--text)" }}>Image Generation</div>

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Model</label>
        <MediaModelPicker value={model} onChange={handleModelChange}
          catalog={catalog.length ? catalog : IMAGE_MODEL_CATALOG} />
      </div>

      {caps?.note && (
        <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 8,
          padding: "6px 10px", background: "rgba(255,255,255,0.03)",
          borderRadius: 6, border: "1px solid rgba(255,255,255,0.07)" }}>
          {caps.note}
        </div>
      )}

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Prompt</label>
        <textarea
          value={prompt} onChange={e => setPrompt(e.target.value)}
          placeholder="A futuristic city at dusk, cinematic lighting…"
          rows={3}
          style={{ ...inputStyle, resize: "vertical", lineHeight: 1.5 }}
        />
      </div>

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Negative prompt (optional)</label>
        <input value={neg} onChange={e => setNeg(e.target.value)}
          placeholder="blurry, bad quality, watermark"
          style={inputStyle} />
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 14 }}>
        <div style={{ flex: 1 }}>
          <label style={labelStyle}>Size</label>
          <select value={size} onChange={e => setSize(e.target.value)} style={{
            ...inputStyle, width: "auto", cursor: "pointer",
          }}>
            {sizes.map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>

        <button onClick={generate} disabled={busy || !prompt.trim()} style={{
          marginTop: 18, padding: "9px 22px", borderRadius: 9, border: "none",
          fontWeight: 600, fontSize: 13, cursor: busy || !prompt.trim() ? "not-allowed" : "pointer",
          background: busy || !prompt.trim()
            ? "rgba(255,255,255,0.06)"
            : "linear-gradient(135deg, #a78bfa 0%, #818cf8 100%)",
          color: busy || !prompt.trim() ? "var(--muted)" : "#fff",
          transition: "opacity 0.15s",
          boxShadow: busy || !prompt.trim() ? "none" : "0 4px 14px rgba(167,139,250,0.35)",
        }}>
          {busy ? "Submitting…" : "Generate"}
        </button>
      </div>

      {error && (
        <div style={{ color: "var(--red)", fontSize: 12, padding: "8px 12px", marginBottom: 10,
          background: "rgba(239,68,68,0.1)", borderRadius: 6, border: "1px solid rgba(239,68,68,0.3)" }}>
          {error}
        </div>
      )}

      {jobs.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 6 }}>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button onClick={() => setJobs([])} style={{
              background: "none", border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: 7, color: "var(--muted)", fontSize: 11,
              padding: "3px 10px", cursor: "pointer",
            }}>clear all</button>
          </div>
          {jobs.map(job => (
            <div key={job.id} style={{
              borderRadius: 12, background: "rgba(255,255,255,0.03)",
              border: "1px solid rgba(255,255,255,0.08)", padding: 12,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--muted)" }}>
                  {job.id.slice(0, 8)}
                </span>
                <StatusPill status={job.status} />
                {(job.status === "queued" || job.status === "processing") && (
                  <span style={{ color: "var(--muted)", fontSize: 11 }}>
                    {job.status === "queued" ? "Waiting for a media agent…" : "Generating…"}
                  </span>
                )}
                {(job.status === "completed" || job.status === "failed") && (
                  <button onClick={() => setJobs(prev => prev.filter(j => j.id !== job.id))} style={{
                    marginLeft: "auto", background: "none", border: "none",
                    color: "var(--muted)", fontSize: 14, cursor: "pointer", lineHeight: 1, padding: "0 2px",
                  }}>×</button>
                )}
              </div>
              {job.status === "completed" && job.data && (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 8 }}>
                  {job.data.map((img, i) => (
                    <div key={i} style={{
                      borderRadius: 8, overflow: "hidden", aspectRatio: "1",
                      background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)",
                    }}>
                      <AuthImage src={img.url} apiKey={apiKey} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                    </div>
                  ))}
                </div>
              )}
              {job.status === "failed" && job.error && (
                <div style={{ color: "var(--red)", fontSize: 12 }}>{job.error}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function VideoGen({ apiKey }: { apiKey: string }) {
  const [model, setModel]         = useState("ltx-video");
  const [prompt, setPrompt]       = useState("");
  const [neg, setNeg]             = useState("");
  const [duration, setDuration]   = useState(5);
  const [size, setSize]           = useState("512x512");
  const [busy, setBusy]           = useState(false);
  const [error, setError]         = useState("");
  const [videos, setVideos]       = useLocalStorage<VideoGenResponse[]>("swarm_video_jobs", []);
  const [catalog, setCatalog]     = useState<MediaModel[]>([]);
  const pollRefs = useRef<Record<string, number>>({});

  useEffect(() => {
    fetch("/swarm/api/v1/video/models", { headers: { "X-API-Key": apiKey } })
      .then(r => r.json()).then(d => setCatalog(d.data ?? [])).catch(() => {});
  }, [apiKey]);

  const caps = catalog.find(m => m.id === model);
  const durMin  = caps?.duration_min  ?? 1;
  const durMax  = caps?.duration_max  ?? 30;
  const sizes   = caps?.sizes ?? ["512x512"];
  const fixedSize = sizes.length === 1;
  const supportsNeg = caps?.supports_neg ?? true;
  const fixedDuration = durMin === durMax;

  // When model changes, snap duration and size to model defaults
  function handleModelChange(id: string) {
    setModel(id);
    const c = catalog.find(m => m.id === id);
    if (c) {
      setDuration(c.duration_default ?? c.duration_min ?? 5);
      setSize(c.sizes?.[0] ?? "512x512");
    }
  }

  function pollVideo(id: string) {
    if (pollRefs.current[id]) return;
    pollRefs.current[id] = window.setInterval(async () => {
      try {
        const res = await fetch(`/swarm/api/v1/video/generations/${id}`, {
          headers: { "X-API-Key": apiKey },
        });
        if (res.status === 404) {
          setVideos(prev => prev.map(v => v.id === id ? { ...v, status: "failed" as const, error: "Job expired (coordinator restarted)" } : v));
          clearInterval(pollRefs.current[id]); delete pollRefs.current[id]; return;
        }
        if (!res.ok) return;
        const data = await res.json() as VideoGenResponse;
        setVideos(prev => prev.map(v => v.id === id ? data : v));
        if (data.status === "completed" || data.status === "failed") {
          clearInterval(pollRefs.current[id]);
          delete pollRefs.current[id];
        }
      } catch { }
    }, 4000);
  }

  useEffect(() => {
    videos.forEach(v => { if (v.status === "queued" || v.status === "processing") pollVideo(v.id); });
    return () => { Object.values(pollRefs.current).forEach(clearInterval); };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function generate() {
    if (!prompt.trim() || busy) return;
    setBusy(true); setError("");
    const [w, h] = size.split("x").map(Number);
    try {
      const res = await fetch("/swarm/api/v1/video/generations", {
        method: "POST",
        headers: { "X-API-Key": apiKey, "Content-Type": "application/json" },
        body: JSON.stringify({ model, prompt: prompt.trim(), negative_prompt: neg,
                               duration, width: w, height: h }),
      });
      if (!res.ok) {
        const e = await res.json().catch(() => ({})) as { detail?: string };
        throw new Error(e.detail ?? `HTTP ${res.status}`);
      }
      const data = await res.json() as VideoGenResponse;
      setVideos(prev => [data, ...prev]);
      pollVideo(data.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 14, color: "var(--text)" }}>Video Generation</div>

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Model</label>
        <MediaModelPicker value={model} onChange={handleModelChange}
          catalog={catalog.length ? catalog : VIDEO_MODEL_CATALOG} />
      </div>

      {caps?.note && (
        <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 8,
          padding: "6px 10px", background: "rgba(255,255,255,0.03)",
          borderRadius: 6, border: "1px solid rgba(255,255,255,0.07)" }}>
          {caps.note}
        </div>
      )}

      <div style={{ marginBottom: 10 }}>
        <label style={labelStyle}>Prompt</label>
        <textarea
          value={prompt} onChange={e => setPrompt(e.target.value)}
          placeholder="A sunset timelapse over mountain peaks, cinematic…"
          rows={3}
          style={{ ...inputStyle, resize: "vertical", lineHeight: 1.5 }}
        />
      </div>

      {supportsNeg && (
        <div style={{ marginBottom: 10 }}>
          <label style={labelStyle}>Negative prompt</label>
          <input value={neg} onChange={e => setNeg(e.target.value)}
            placeholder="blurry, static, distorted"
            style={inputStyle} />
        </div>
      )}

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 14, alignItems: "flex-end" }}>
        {!fixedDuration && (
          <div>
            <label style={labelStyle}>Duration — {duration}s</label>
            <input type="range" min={durMin} max={durMax} value={duration}
              onChange={e => setDuration(Number(e.target.value))}
              style={{ width: 120, accentColor: "#22d3ee" }} />
          </div>
        )}
        {fixedDuration && (
          <div style={{ fontSize: 11, color: "var(--muted)" }}>
            Duration: <strong style={{ color: "var(--text)" }}>{durMin}s</strong> (fixed)
          </div>
        )}

        {!fixedSize && (
          <div>
            <label style={labelStyle}>Resolution</label>
            <select value={size} onChange={e => setSize(e.target.value)}
              style={{ ...inputStyle, paddingRight: 8 }}>
              {sizes.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        )}
        {fixedSize && (
          <div style={{ fontSize: 11, color: "var(--muted)" }}>
            Resolution: <strong style={{ color: "var(--text)" }}>{sizes[0]}</strong> (fixed)
          </div>
        )}

        <div>
          <button onClick={generate} disabled={busy || !prompt.trim()} style={{
            padding: "9px 22px", borderRadius: 9, border: "none",
            fontWeight: 600, fontSize: 13, cursor: busy || !prompt.trim() ? "not-allowed" : "pointer",
            background: busy || !prompt.trim()
              ? "rgba(255,255,255,0.06)"
              : "linear-gradient(135deg, #22d3ee 0%, #0ea5e9 100%)",
            color: busy || !prompt.trim() ? "var(--muted)" : "#fff",
            boxShadow: busy || !prompt.trim() ? "none" : "0 4px 14px rgba(34,211,238,0.3)",
          }}>
            {busy ? "Submitting…" : "Generate"}
          </button>
        </div>
      </div>

      {error && (
        <div style={{ color: "var(--red)", fontSize: 12, padding: "8px 12px", marginBottom: 10,
          background: "rgba(239,68,68,0.1)", borderRadius: 6, border: "1px solid rgba(239,68,68,0.3)" }}>
          {error}
        </div>
      )}

      {videos.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 6 }}>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button onClick={() => setVideos([])} style={{
              background: "none", border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: 7, color: "var(--muted)", fontSize: 11,
              padding: "3px 10px", cursor: "pointer",
            }}>clear all</button>
          </div>
          {videos.map(v => (
            <div key={v.id} style={{
              borderRadius: 12, overflow: "hidden",
              background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.08)",
              padding: 12,
            }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--muted)" }}>
                  {v.id.slice(0, 8)}
                </span>
                <StatusPill status={v.status} />
                {v.status === "queued" && (
                  <span style={{ color: "var(--muted)", fontSize: 11 }}>Waiting for a media agent…</span>
                )}
                {v.status === "processing" && !v.progress && (
                  <span style={{ color: "var(--muted)", fontSize: 11 }}>
                    {v.model ? `Loading model…` : "Generating…"}
                  </span>
                )}
                {(v.status === "completed" || v.status === "failed") && (
                  <button onClick={() => setVideos(prev => prev.filter(x => x.id !== v.id))} style={{
                    marginLeft: "auto", background: "none", border: "none",
                    color: "var(--muted)", fontSize: 14, cursor: "pointer", lineHeight: 1, padding: "0 2px",
                  }}>×</button>
                )}
              </div>
              {v.status === "processing" && v.progress && (
                <ProgressBar progress={v.progress} />
              )}
              {v.status === "completed" && v.url && (
                <>
                  <AuthVideo src={v.url} apiKey={apiKey} style={{ width: "100%", maxHeight: 320, borderRadius: 6 }} />
                  <AuthVideoDownload src={v.url} apiKey={apiKey} jobId={v.id} />
                </>
              )}
              {v.status === "failed" && v.error && (
                <div style={{ color: "var(--red)", fontSize: 12 }}>{v.error}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ProgressBar({ progress }: { progress: JobProgress }) {
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4,
        fontSize: 10, color: "var(--muted)" }}>
        <span>Step {progress.step} / {progress.total}</span>
        <span>{progress.pct}%</span>
      </div>
      <div style={{ height: 4, borderRadius: 99, background: "rgba(255,255,255,0.08)", overflow: "hidden" }}>
        <div style={{
          height: "100%", borderRadius: 99,
          width: `${progress.pct}%`,
          background: "linear-gradient(90deg, #22d3ee, #0ea5e9)",
          transition: "width 0.4s ease",
        }} />
      </div>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const colors: Record<string, [string, string]> = {
    queued:     ["rgba(251,191,36,.12)",  "var(--yellow)"],
    processing: ["rgba(167,139,250,.12)", "var(--accent)"],
    completed:  ["rgba(52,211,153,.1)",   "var(--green)"],
    failed:     ["rgba(248,113,113,.1)",  "var(--red)"],
  };
  const [bg, color] = colors[status] ?? ["rgba(255,255,255,.05)", "var(--muted)"];
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      background: bg, color, borderRadius: 99, padding: "2px 9px",
      fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.07em",
    }}>
      <span style={{ width: 5, height: 5, borderRadius: "50%", background: color,
        animation: status === "processing" || status === "queued" ? "pulse 1.5s infinite" : undefined }} />
      {status}
    </span>
  );
}

function MediaJobsPanel({ apiKey }: { apiKey: string }) {
  const [jobs, setJobs]   = useState<MediaJob[]>([]);
  const [open, setOpen]   = useState(true);

  const refresh = useCallback(async () => {
    try {
      const res = await apiFetch("/v1/media/jobs", apiKey);
      if (res.ok) {
        const data = await res.json() as MediaJobsResponse;
        setJobs(data.jobs);
      }
    } catch {}
  }, [apiKey]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 4000);
    return () => clearInterval(id);
  }, [refresh]);

  const statusColor = (s: string) =>
    s === "completed" ? "var(--green)"
    : s === "failed"  ? "var(--red)"
    : s === "processing" ? "var(--accent)"
    : "var(--yellow)";

  const statusDot = (s: string) => (
    <span style={{
      display: "inline-block", width: 7, height: 7, borderRadius: "50%",
      background: statusColor(s),
      boxShadow: s === "processing" ? `0 0 6px ${statusColor(s)}` : "none",
      marginRight: 6, flexShrink: 0,
      animation: s === "processing" ? "pulse 2s infinite" : "none",
    }} />
  );

  const queued     = jobs.filter(j => j.status === "queued").length;
  const processing = jobs.filter(j => j.status === "processing").length;

  return (
    <div style={{
      background: "rgba(255,255,255,0.02)",
      border: "1px solid rgba(255,255,255,0.08)",
      borderRadius: 14, marginTop: 28, overflow: "hidden",
    }}>
      <div
        onClick={() => setOpen(o => !o)}
        style={{
          display: "flex", alignItems: "center", gap: 12,
          padding: "12px 18px", cursor: "pointer",
          borderBottom: open ? "1px solid rgba(255,255,255,0.06)" : "none",
          userSelect: "none",
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text)" }}>
          Job Queue
        </span>
        {queued > 0 && (
          <span style={{
            background: "rgba(250,204,21,0.15)", color: "var(--yellow)",
            border: "1px solid rgba(250,204,21,0.3)",
            fontSize: 10, fontWeight: 700, borderRadius: 99, padding: "1px 8px",
          }}>{queued} queued</span>
        )}
        {processing > 0 && (
          <span style={{
            background: "rgba(167,139,250,0.15)", color: "var(--accent)",
            border: "1px solid rgba(167,139,250,0.3)",
            fontSize: 10, fontWeight: 700, borderRadius: 99, padding: "1px 8px",
          }}>{processing} processing</span>
        )}
        <span style={{ marginLeft: "auto", color: "var(--muted)", fontSize: 11 }}>
          {jobs.length} total  {open ? "▲" : "▼"}
        </span>
      </div>

      {open && (
        <div style={{ overflowX: "auto" }}>
          {jobs.length === 0 ? (
            <div style={{ padding: "28px 18px", color: "var(--muted)", fontSize: 13, textAlign: "center" }}>
              No jobs yet — generate an image or video above.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr>
                  {["", "Type", "Model", "Status", "Prompt", "Duration", "Result"].map(h => (
                    <th key={h} style={{
                      padding: "8px 14px", textAlign: "left", fontWeight: 600,
                      fontSize: 10, letterSpacing: "0.06em", textTransform: "uppercase",
                      color: "var(--muted)", borderBottom: "1px solid rgba(255,255,255,0.06)",
                      whiteSpace: "nowrap",
                    }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {jobs.map((job, i) => (
                  <tr key={job.id} style={{ borderBottom: i < jobs.length - 1 ? "1px solid rgba(255,255,255,0.04)" : "none" }}>
                    <td style={{ padding: "8px 14px", width: 24 }}>
                      {statusDot(job.status)}
                    </td>
                    <td style={{ padding: "8px 14px", color: "var(--muted)", fontFamily: "var(--mono)", whiteSpace: "nowrap" }}>
                      {job.type === "image" ? "🖼" : "🎬"} {job.type}
                    </td>
                    <td style={{ padding: "8px 14px", fontFamily: "var(--mono)", color: "var(--accent)", whiteSpace: "nowrap" }}>
                      {job.model}
                    </td>
                    <td style={{ padding: "8px 14px", whiteSpace: "nowrap" }}>
                      <span style={{ color: statusColor(job.status) }}>
                        {job.status}
                        {job.status === "processing" && job.progress
                          ? ` ${job.progress.pct}%`
                          : ""}
                      </span>
                      {job.status === "failed" && job.error && (
                        <div style={{ color: "var(--red)", fontSize: 10, marginTop: 2, maxWidth: 200 }}>
                          {job.error.slice(0, 120)}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: "8px 14px", color: "var(--text)", maxWidth: 320 }}>
                      <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {job.prompt}
                      </span>
                    </td>
                    <td style={{ padding: "8px 14px", color: "var(--muted)", whiteSpace: "nowrap", fontFamily: "var(--mono)" }}>
                      {job.elapsed_ms != null
                        ? `${(job.elapsed_ms / 1000).toFixed(1)}s`
                        : new Date(job.created * 1000).toLocaleTimeString()}
                    </td>
                    <td style={{ padding: "8px 14px" }}>
                      {job.status === "completed" && job.url && (
                        job.type === "image"
                          ? <a href={job.url} target="_blank" rel="noopener noreferrer">
                              <img src={job.url} alt="" style={{ height: 48, borderRadius: 4, border: "1px solid rgba(255,255,255,0.12)", objectFit: "cover" }} />
                            </a>
                          : <a href={job.url} target="_blank" rel="noopener noreferrer"
                              style={{ color: "var(--accent)", textDecoration: "none", fontSize: 11 }}>
                              ▶ view
                            </a>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

function MediaView({ apiKey }: { apiKey: string }) {
  return (
    <div style={{
      background: "rgba(255,255,255,0.025)",
      backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)",
      borderRadius: 16, border: "1px solid rgba(255,255,255,0.08)",
      padding: "24px 28px",
    }}>
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 11, color: "var(--muted)", lineHeight: 1.6, maxWidth: 640 }}>
          Requires a media agent:{" "}
          <code style={{ fontFamily: "var(--mono)", color: "var(--accent)", fontSize: 11 }}>
            .\agent.ps1 -MediaModels "flux-schnell,ltx-video"
          </code>
          {" "}— the agent installs Python + diffusers automatically on first run.
          Images are returned synchronously; videos poll asynchronously.
        </div>
      </div>
      <div style={{ display: "flex", gap: 32, flexWrap: "wrap", alignItems: "flex-start" }}>
        <ImageGen apiKey={apiKey} />
        <div style={{ width: 1, background: "rgba(255,255,255,0.07)", alignSelf: "stretch", flexShrink: 0 }} />
        <VideoGen apiKey={apiKey} />
      </div>
      <MediaJobsPanel apiKey={apiKey} />
    </div>
  );
}

// ── dashboard ─────────────────────────────────────────────────────────────────

type Tab = "machines" | "chat" | "usage" | "media";

function Dashboard({ apiKey, onLogout }: { apiKey: string; onLogout: () => void }) {
  const [data, setData]               = useState<MachinesResponse | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [error, setError]             = useState<string | null>(null);
  const [tab, setTab]                 = useState<Tab>("machines");

  const fetchData = useCallback(async () => {
    try {
      const res = await apiFetch("/machines", apiKey);
      if (res.status === 401) { onLogout(); return; }
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setData(await res.json() as MachinesResponse);
      setLastUpdated(new Date());
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }, [apiKey, onLogout]);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, POLL_MS);
    return () => clearInterval(id);
  }, [fetchData]);

  const totalOk  = data?.machines.reduce((s, m) => s + m.requests_ok,  0) ?? 0;
  const totalErr = data?.machines.reduce((s, m) => s + m.requests_err, 0) ?? 0;

  return (
    <div style={{ padding: "28px 36px", maxWidth: 1440, margin: "0 auto" }}>
      {/* header */}
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 10 }}>
        <h1 style={{
          fontSize: 22, fontWeight: 700, letterSpacing: "-0.04em",
          background: "linear-gradient(135deg, #a78bfa 0%, #22d3ee 100%)",
          WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
        }}>
          swarm-llm
        </h1>
        <span style={{
          color: "var(--muted)", fontSize: 11, fontWeight: 500,
          border: "1px solid rgba(255,255,255,0.08)", borderRadius: 99,
          padding: "2px 10px", letterSpacing: "0.04em",
        }}>control plane</span>

        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 12 }}>
          {error ? (
            <span style={{ color: "var(--red)", fontSize: 12 }}>⚠ {error}</span>
          ) : (
            <span style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--green)", fontSize: 12 }}>
              <span style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--green)",
                boxShadow: "0 0 8px var(--green)", animation: "pulse 2.5s infinite" }} />
              live
            </span>
          )}
          {lastUpdated && (
            <span style={{ color: "var(--muted)", fontSize: 11, fontFamily: "var(--mono)" }}>
              {lastUpdated.toLocaleTimeString()}
            </span>
          )}
          <button onClick={onLogout} style={{
            background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.1)",
            borderRadius: 8, color: "var(--muted)", fontSize: 11,
            padding: "4px 12px", cursor: "pointer", transition: "color 0.15s",
          }}>disconnect</button>
        </div>
      </div>

      {/* endpoint banner */}
      <div style={{
        background: "rgba(167,139,250,0.07)",
        border: "1px solid rgba(167,139,250,0.2)", borderRadius: 10,
        padding: "8px 16px", fontFamily: "var(--mono)", fontSize: 12,
        color: "var(--accent)", marginBottom: 28, display: "inline-flex",
        alignItems: "center", gap: 8,
      }}>
        <span style={{ opacity: 0.5, fontSize: 10 }}>OpenAI endpoint</span>
        <span style={{ opacity: 0.35 }}>→</span>
        {deployment.openai}
        <span style={{ color: "var(--muted)", opacity: 0.7, marginLeft: 8, fontSize: 11 }}>Use a consumer API key</span>
      </div>

      {/* stat strip */}
      <div style={{
        display: "flex", alignItems: "stretch", marginBottom: 28,
        background: "rgba(255,255,255,0.025)",
        backdropFilter: "blur(16px)", WebkitBackdropFilter: "blur(16px)",
        border: "1px solid rgba(255,255,255,0.08)",
        borderRadius: 14, overflow: "hidden",
      }}>
        <StatCard value={data?.alive   ?? 0} label="Live"        color="var(--green)"  />
        <div style={{ width: 1, background: "rgba(255,255,255,0.07)", flexShrink: 0 }} />
        <StatCard value={data?.machines.filter(m => m.alive && m.busy).length ?? 0} label="Processing" color="var(--accent)" />
        <div style={{ width: 1, background: "rgba(255,255,255,0.07)", flexShrink: 0 }} />
        <StatCard value={data?.queued  ?? 0} label="Queued"      color={(data?.queued ?? 0) > 0 ? "var(--yellow)" : "var(--muted)"} glow />
        <div style={{ width: 1, background: "rgba(255,255,255,0.07)", flexShrink: 0 }} />
        <StatCard value={data?.pending ?? 0} label="Setting up"  color="var(--yellow)" glow />
        <div style={{ width: 1, background: "rgba(255,255,255,0.07)", flexShrink: 0 }} />
        <StatCard value={data?.dead    ?? 0} label="Dead"        color="var(--red)"    glow />
        <div style={{ width: 1, background: "rgba(255,255,255,0.14)", flexShrink: 0 }} />
        <StatCard value={data?.total   ?? 0} label="Registered"  color="var(--muted)"  />
        <div style={{ width: 1, background: "rgba(255,255,255,0.07)", flexShrink: 0 }} />
        <StatCard value={totalOk}            label="Requests OK"  color="var(--green)"  />
        <div style={{ width: 1, background: "rgba(255,255,255,0.07)", flexShrink: 0 }} />
        <StatCard value={totalErr}           label="Errors"       color="var(--red)"    glow />
      </div>

      {/* tab switcher */}
      <div style={{
        display: "inline-flex", gap: 3, marginBottom: 20, padding: "4px",
        background: "rgba(255,255,255,0.04)", borderRadius: 11,
        border: "1px solid rgba(255,255,255,0.08)",
      }}>
        {(["machines", "chat", "media", "usage"] as Tab[]).map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            padding: "6px 20px", borderRadius: 8, fontSize: 12, fontWeight: 600,
            border: "none", cursor: "pointer",
            background: tab === t
              ? "linear-gradient(135deg, rgba(167,139,250,0.25), rgba(34,211,238,0.15))"
              : "transparent",
            color: tab === t ? "var(--text)" : "var(--muted)",
            boxShadow: tab === t ? "0 0 0 1px rgba(167,139,250,0.3)" : "none",
            transition: "all 0.15s",
          }}>
            {t === "machines" ? "Machines" : t === "chat" ? "Chat" : t === "media" ? "Media" : "Usage"}
          </button>
        ))}
      </div>

      {/* tab content */}
      {tab === "usage" ? (
        <UsageView apiKey={apiKey} />
      ) : tab === "media" ? (
        <MediaView apiKey={apiKey} />
      ) : tab === "machines" ? (
        <>
        <ReplicaOverview clusters={data?.rpc_clusters ?? []} machines={data?.machines ?? []} />
        <div style={{
          background: "rgba(255,255,255,0.02)",
          backdropFilter: "blur(14px)", WebkitBackdropFilter: "blur(14px)",
          borderRadius: 16, border: "1px solid rgba(255,255,255,0.08)", overflowX: "auto",
        }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                {["Status","Machine","Models / progress","CPU","RAM (free/total)","GPU","Media","Network","Req OK / Err","Avg latency","Last seen"]
                  .map(h => <th key={h} style={th}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {data && data.machines.length > 0
                ? [...data.machines]
                    .sort((a, b) => (b.alive ? 1 : 0) - (a.alive ? 1 : 0))
                    .map(m => <MachineRow key={m.key} m={m} cluster={machineCluster(m, data.rpc_clusters ?? [])} />)
                : (
                  <tr><td colSpan={11} style={{ ...td, textAlign: "center",
                    color: "var(--muted)", padding: "48px 0", fontSize: 13 }}>
                    No agents connected yet. Run{" "}
                    <code style={{ fontFamily: "var(--mono)", color: "var(--accent)" }}>agent.ps1</code>
                    {" "}on a Windows machine to join the swarm.
                  </td></tr>
                )
              }
            </tbody>
          </table>
        </div>
        </>
      ) : (
        <Chat apiKey={apiKey} />
      )}
    </div>
  );
}

// ── root ──────────────────────────────────────────────────────────────────────

export default function App() {
  const [apiKey, setApiKey] = useState<string | null>(
    () => localStorage.getItem(LS_KEY)
  );

  function handleLogout() {
    localStorage.removeItem(LS_KEY);
    setApiKey(null);
  }

  if (!apiKey) {
    return <LoginScreen onLogin={setApiKey} />;
  }
  return <Dashboard apiKey={apiKey} onLogout={handleLogout} />;
}
