import { useCallback, useEffect, useRef, useState } from "react"
import { Brand, Choice, Tabs, Hold, NoticeLine, Panel, Readout, Screen, Spacer, StateDot, Tag, ThemeSwitch } from "@neon"
import { RunControl, type Rcc } from "./RunControl"
import { Settings } from "./Settings"
import { History } from "./History"
import { Configuration } from "./Configuration"
import { humanDuration, humanSize, localTime, type Attempt, type DataFile } from "./lib"

/* The shapes the backend sends. Kept minimal on purpose: this page renders the
   run status and nothing else, and a field it does not draw is a field that
   cannot go stale on it. */

type ProcessState = {
  label: string
  running: boolean
  pid: number | null
  since: string | null
}



type Status = {
  now: string
  live: Attempt | null
  rate_bytes_per_s: number | null
  attempts: number
  last_poll: string | null
  log_error: string | null
  processes: ProcessState[]
  daq_up: boolean
  acq_root: string
  container: string
  rcc: Rcc | null
}

/** Remembered per browser: a controller who chose to just watch stays watching. */
const MODE_KEY = "ganil.mode"
type Mode = "control" | "monitor"




export default function App() {
  const [status, setStatus] = useState<Status | null>(null)
  const [attempts, setAttempts] = useState<Attempt[]>([])
  const [orphans, setOrphans] = useState<DataFile[]>([])
  const [runDir, setRunDir] = useState("")
  const [connected, setConnected] = useState(false)
  const lastRunKey = useRef<string | null>(null)
  // One service, two modes (as minicom): who may control is the server's call
  // (/api/me, enforced on every POST); anyone who may can step down to Monitor.
  const [mayControl, setMayControl] = useState(false)
  const [chosen, setChosen] = useState<Mode>(() => {
    try {
      return window.localStorage.getItem(MODE_KEY) === "monitor" ? "monitor" : "control"
    } catch {
      return "control"
    }
  })
  const control = mayControl && chosen === "control"
  const [tab, setTab] = useState("run")
  const showSettings = control && tab === "settings"
  const showHistory = tab === "history"
  const showConfig = tab === "config"
  useEffect(() => {
    const ask = () =>
      fetch("api/me")
        .then((r) => (r.ok ? r.json() : null))
        .then((me) => setMayControl(!!me?.control))
        .catch(() => undefined)
    void ask()
    const timer = setInterval(ask, 30000)
    return () => clearInterval(timer)
  }, [])
  const chooseMode = (next: Mode) => {
    setChosen(next)
    try {
      window.localStorage.setItem(MODE_KEY, next)
    } catch {
      /* just not remembered */
    }
  }

  const loadRuns = useCallback(async () => {
    try {
      const answer = await fetch("api/runs?limit=500")
      if (!answer.ok) return
      const data = await answer.json()
      setAttempts(data.attempts ?? [])
      setOrphans(data.orphan_files ?? [])
      setRunDir(data.run_dir ?? "")
    } catch {
      /* The socket already tells the operator the service is unreachable;
         a second banner saying so adds nothing. */
    }
  }, [])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  // The live view arrives on a socket; the catalog is refetched only when the
  // run boundary moves, which is the only thing that can change it.
  useEffect(() => {
    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let closed = false

    const open = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws"
      const base = new URL("ws/status", window.location.href)
      base.protocol = `${scheme}:`
      socket = new WebSocket(base.href)
      socket.onopen = () => setConnected(true)
      socket.onmessage = (event) => {
        const next = JSON.parse(event.data) as Status
        setStatus(next)
        const key = next.live ? `${next.live.run}@${next.live.started}` : "idle"
        if (key !== lastRunKey.current) {
          lastRunKey.current = key
          void loadRuns()
        }
      }
      socket.onclose = () => {
        setConnected(false)
        if (!closed) retry = setTimeout(open, 3000)
      }
      socket.onerror = () => socket?.close()
    }
    open()
    return () => {
      closed = true
      if (retry) clearTimeout(retry)
      socket?.close()
    }
  }, [loadRuns])

  const live = status?.live ?? null
  const rate = status?.rate_bytes_per_s ?? null

  return (
    <Screen
      top={
        <>
          {/* Monitoring is stated, not implied by missing buttons. */}
          <Brand name="GANIL DAQ" readOnly={!control} />
          {/* Settings change what is read and whom is asked: a controller's tab. */}
          <Tabs
            items={[
              { value: "run", label: "Run" },
              { value: "history", label: "History", state: attempts.length ? `${attempts.length} runs` : undefined },
              { value: "config", label: "Configuration" },
              ...(control ? [{ value: "settings", label: "Settings" }] : []),
            ]}
            value={showSettings || showHistory || showConfig ? tab : "run"}
            onChange={setTab}
          />
          {/* The experiment in use, as a fact (a tag, not a tab): its whole path is in the tooltip and in Settings. */}
          <span title={status?.acq_root}>
            <Tag label="EXP">{status?.acq_root ? status.acq_root.replace(/\/acquisition\/?$/, "").split("/").pop() : "…"}</Tag>
          </span>
          <Spacer />
          {/* Every wording reserved, so the bar never shifts (rule 5). */}
          <StateDot tone={status?.daq_up ? "ok" : "alarm"} reserve={["DAQ up", "DAQ down"]}>
            {status?.daq_up ? "DAQ up" : "DAQ down"}
          </StateDot>
          <StateDot tone={connected ? "ok" : "warn"} reserve={["live", "reconnecting"]}>
            {connected ? "live" : "reconnecting"}
          </StateDot>
          {mayControl ? (
            <Choice
              small
              options={[
                { value: "control", label: "Control" },
                { value: "monitor", label: "Monitor" },
              ]}
              value={control ? "control" : "monitor"}
              onChange={chooseMode}
            />
          ) : null}
          <ThemeSwitch />
        </>
      }
    >
      {showSettings ? <Settings /> : showHistory ? <History attempts={attempts} orphans={orphans} runDir={runDir} /> : showConfig ? <Configuration path={status?.rcc?.config_file ?? null} /> : <div className="gn-run">
        {/* One screen: the run now and the run control side by side, the
            runs below them taking whatever height is left. */}
        <div className="gn-top">
          {/* ── The run going now: the same box whether one is or not ─────── */}
          <Panel
            title={
              // The run's number is a fact, lit while the run is open (rule 4).
              <span className="gn-tags">
                Run now
                <Tag label="RUN" on={!!live}>{live ? live.run : "—"}</Tag>
                <Tag label="STARTED">
                  <Hold reserve={["8888-88-88 오후 88:88:88", "—"]}>{live ? localTime(live.started) : "—"}</Hold>
                </Tag>
              </span>
            }
            actions={
              <StateDot tone={live ? "ok" : "idle"} reserve={["running", "no run in progress"]}>
                {live ? "running" : "no run in progress"}
              </StateDot>
            }
          >
            {/* The note is the reason the run exists, and the one part of a
                GANIL run nobody can reconstruct afterwards. */}
            <div className={`gn-note${live?.note ? "" : " faint"}`}>
              {live ? live.note || "started with no note" : `${status?.attempts ?? 0} attempts logged — the next run's note shows here.`}
            </div>
            {/* The four numbers a shifter reads from across the room: big, two by two. */}
            {/* What a shifter reads from across the room: the run and how long it has gone, biggest;
                Elapsed in the accent, the one number that moves every second. */}
            <div className="gn-big">
              <Readout size="hero" label="Run" value={<Hold reserve={["8888"]}>{live ? String(live.run) : "—"}</Hold>} />
              <div className="gn-elapsed">
                <Readout size="hero" label="Elapsed" value={<Hold reserve={["88h 88m"]}>{live ? humanDuration(live.elapsed_s) : "—"}</Hold>} />
              </div>
            </div>
            <div className="gn-mid">
              <Readout label="Written" value={<Hold reserve={["888.8 GB"]}>{live ? humanSize(live.size) : "—"}</Hold>} />
              <Readout
                label="Rate"
                value={<Hold reserve={["888.8 MB/s", "measuring…"]}>{!live ? "—" : rate === null ? "measuring…" : `${humanSize(rate)}/s`}</Hold>}
              />
              <Readout label="Files" value={<Hold reserve={["888"]}>{live ? String(live.files.length) : "—"}</Hold>} />
            </div>
            {/* One line always (rule 5): a log that cannot be read, the files, or why there are none. */}
            {status?.log_error ? (
              <NoticeLine tone="alarm">{status.log_error}</NoticeLine>
            ) : (
              <div className={`gn-files${live && live.files.length === 0 ? " tone-text-warn" : ""}`} title={live?.files.map((f) => f.name).join("\n")}>
                {!live
                  ? "Run-control logs read"
                  : live.files.length > 0
                    ? live.files.map((f) => f.name).join("  ·  ")
                    : "No data file — storage is off for this run, or not written yet."}
              </div>
            )}
          </Panel>

          {/* ── The run control and the container's processes ─────────────── */}
          <RunControl
            rcc={status?.rcc ?? null}
            control={control}
            processes={(status?.processes ?? []).map((process) => ({
              label: process.label,
              on: process.running,
              tone: process.running ? ("ok" as const) : ("alarm" as const),
              title: process.running ? `pid ${process.pid}` : "not running",
            }))}
            container={status?.container ?? "the DAQ container"}
            onOpenConfig={() => setTab("config")}
          />
        </div>

        {/* The last few runs, so the screen below the numbers says what came before. The whole list, files and all, is History. */}
        <Panel
          fill
          title="Recent runs"
          actions={
            <button type="button" className="neon-theme" onClick={() => setTab("history")}>
              all {attempts.length} in History →
            </button>
          }
        >
          <div className="gn-runs">
            <table className="gn-recent">
              <tbody>
                {attempts.slice(0, 30).map((attempt) => (
                  <tr key={`${attempt.run}@${attempt.started}`}>
                    <td className="tabular">
                      <b>{attempt.run}</b>
                      {attempt.live ? <span className="tone-text-ok"> live</span> : null}
                    </td>
                    <td className="tabular faint">{localTime(attempt.started)}</td>
                    <td className="tabular">{humanDuration(attempt.elapsed_s)}</td>
                    <td className="tabular gn-right">{attempt.files.length ? humanSize(attempt.size) : <span className="faint">{attempt.storage_off ? "storage off" : "no file"}</span>}</td>
                    <td className="gn-recent-note">{attempt.note.split("\n")[0]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

      </div>}
    </Screen>
  )
}
