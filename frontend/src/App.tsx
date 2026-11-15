import { useCallback, useEffect, useRef, useState } from "react"
import { Table } from "@mantine/core"
import { Brand, Flags, Hold, NoticeLine, Panel, Readout, Screen, Spacer, StateDot, Tag, ThemeSwitch } from "@neon"

/* The shapes the backend sends. Kept minimal on purpose: this page renders the
   run status and nothing else, and a field it does not draw is a field that
   cannot go stale on it. */

type ProcessState = {
  label: string
  running: boolean
  pid: number | null
  since: string | null
}

type DataFile = {
  name: string
  run: number
  started: string
  part: number
  size: number
  modified: string
}

type Attempt = {
  run: number
  started: string
  stopped: string | null
  note: string
  live: boolean
  elapsed_s: number | null
  size: number
  storage_off: boolean
  from_server_log: boolean
  files: DataFile[]
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
}

/** Bytes as an operator reads them. Binary units, because that is what `ls -lh`
    on the run directory says and the two must agree. */
function humanSize(bytes: number): string {
  if (!bytes) return "0 B"
  const units = ["B", "kB", "MB", "GB", "TB"]
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`
}

/** A duration a shifter can read at a glance: 4h 12m, not 15120 s. */
function humanDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—"
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`
  return `${s}s`
}

/** Local time. The logs are on the DAQ container's UTC clock and the backend
    hands them over as aware timestamps, so the browser does the last step and
    the hour shown is the hour the shifter was standing there. */
function localTime(iso: string | null): string {
  if (!iso) return "—"
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? "—" : when.toLocaleString()
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null)
  const [attempts, setAttempts] = useState<Attempt[]>([])
  const [orphans, setOrphans] = useState<DataFile[]>([])
  const [connected, setConnected] = useState(false)
  const lastRunKey = useRef<string | null>(null)

  const loadRuns = useCallback(async () => {
    try {
      const answer = await fetch("api/runs?limit=500")
      if (!answer.ok) return
      const data = await answer.json()
      setAttempts(data.attempts ?? [])
      setOrphans(data.orphan_files ?? [])
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
          {/* It only reads files: there is no control surface here and cannot be one. */}
          <Brand name="GANIL DAQ" readOnly />
          <span className="faint">{status?.acq_root ?? "…"}</span>
          <Spacer />
          {/* Every wording reserved, so the bar never shifts (rule 5). */}
          <StateDot tone={status?.daq_up ? "ok" : "alarm"} reserve={["DAQ up", "DAQ down"]}>
            {status?.daq_up ? "DAQ up" : "DAQ down"}
          </StateDot>
          <StateDot tone={connected ? "ok" : "warn"} reserve={["live", "reconnecting"]}>
            {connected ? "live" : "reconnecting"}
          </StateDot>
          <ThemeSwitch />
        </>
      }
    >
      <div className="gn-stack">
        {/* Always there (rule 5): a log that cannot be read fills this line. */}
        <NoticeLine tone={status?.log_error ? "alarm" : null}>{status?.log_error ?? "Run-control logs read"}</NoticeLine>

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
          <div className="gn-stack">
            {/* The note is the reason the run exists, and the one part of a
                GANIL run nobody can reconstruct afterwards. */}
            <div className={`gn-note${live?.note ? "" : " faint"}`}>
              {live ? live.note || "started with no note" : `${status?.attempts ?? 0} attempts logged — the next run's note shows here.`}
            </div>
            <div className="gn-row">
              <Readout label="Elapsed" value={<Hold reserve={["88h 88m"]}>{live ? humanDuration(live.elapsed_s) : "—"}</Hold>} />
              <Readout label="Written" value={<Hold reserve={["888.8 GB"]}>{live ? humanSize(live.size) : "—"}</Hold>} />
              <Readout
                label="Rate"
                value={<Hold reserve={["888.8 MB/s", "measuring…"]}>{!live ? "—" : rate === null ? "measuring…" : `${humanSize(rate)}/s`}</Hold>}
              />
              <Readout label="Files" value={<Hold reserve={["888"]}>{live ? String(live.files.length) : "—"}</Hold>} />
            </div>
            {/* One line either way: the files, or why there are none. */}
            <div className={`gn-files${live && live.files.length === 0 ? " tone-text-warn" : ""}`} title={live?.files.map((f) => f.name).join("\n")}>
              {!live
                ? "—"
                : live.files.length > 0
                  ? live.files.map((f) => f.name).join("  ·  ")
                  : "No data file yet — storage may be off for this run."}
            </div>
          </div>
        </Panel>

        {/* ── The DAQ's components ──────────────────────────────────────── */}
        <Panel title={`Components in ${status?.container ?? "the DAQ container"}`}>
          <Flags
            items={(status?.processes ?? []).map((process) => ({
              label: process.label,
              on: process.running,
              tone: process.running ? ("ok" as const) : ("alarm" as const),
              title: process.running ? `pid ${process.pid}` : "not running",
            }))}
          />
        </Panel>

        {/* ── Every run taken ───────────────────────────────────────────── */}
        <Panel title="Runs · newest first">
          <div className="gn-runs">
            <Table stickyHeader highlightOnHover verticalSpacing="xs">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th w={150}>Run</Table.Th>
                  <Table.Th w={210}>Started</Table.Th>
                  <Table.Th w={100}>Length</Table.Th>
                  <Table.Th w={110}>Size</Table.Th>
                  <Table.Th>Note</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {attempts.map((attempt) => (
                  <Table.Tr key={`${attempt.run}@${attempt.started}`}>
                    <Table.Td>
                      <span className="gn-tags">
                        <b className="tabular">{attempt.run}</b>
                        {attempt.live ? <span className="tone-text-ok">live</span> : null}
                        {attempt.from_server_log ? (
                          <span
                            className="faint"
                            title="The run-control GUI never logged this run; it was recovered from the server log (rcc.txt), which is why it has no note."
                          >
                            server log
                          </span>
                        ) : null}
                      </span>
                    </Table.Td>
                    <Table.Td className="tabular">{localTime(attempt.started)}</Table.Td>
                    <Table.Td className="tabular">{humanDuration(attempt.elapsed_s)}</Table.Td>
                    <Table.Td className="tabular">
                      {attempt.files.length ? (
                        humanSize(attempt.size)
                      ) : (
                        <span className="faint">{attempt.storage_off ? "storage off" : "no file"}</span>
                      )}
                    </Table.Td>
                    <Table.Td style={{ whiteSpace: "pre-wrap" }}>{attempt.note}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </div>
        </Panel>

        {/* Files on disk that belong to no logged attempt -- last on the page,
            so appearing pushes nothing. Usually an aborted storage, but a run
            whose log lines were rotated away looks exactly the same. */}
        {orphans.length > 0 ? (
          <Panel title={`${orphans.length} file(s) matching no logged run`}>
            {orphans.map((file) => (
              <div key={file.name} className="gn-files">
                {file.name} · {humanSize(file.size)}
              </div>
            ))}
          </Panel>
        ) : null}
      </div>
    </Screen>
  )
}
