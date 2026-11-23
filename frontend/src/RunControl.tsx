import { useState } from "react"
import { Command, Flags, Hold, NoticeLine, Panel, StateDot, Tag, type Tone } from "@neon"

/* What /api/status carries under `rcc`: the run control's state, asked over SOAP. */
export type RccState = { code: number; name: string; transition: number; error: number }
export type Rcc = {
  reachable: boolean
  error: string | null
  at: number | null
  experiment?: string
  config_file?: string
  state?: RccState
  monitoring?: boolean
  run_number?: number | null
  storage?: "on" | "off" | "mixed" | null
  equipment?: { name: string; type: string; state: RccState; storage_on?: boolean }[]
}

const READY = 2
const RUNNING = 3
const PAUSED = 4
/** Which state each command may be sent from -- the server checks the same (rcc.py). */
const ALLOWED: Record<string, number[]> = {
  init: [0, 1, READY],
  breakup: [1, READY, 5, 6, 7],
  "ecc-init": [0, 1],
  "ecc-breakup": [1, READY, 5, 7],
  "storage-on": [READY],
  "storage-off": [READY],
  start: [READY],
  stop: [RUNNING, PAUSED],
}
/** Judged on the ECC's own state; refused while the experiment takes data. */
const ECC_ONLY = new Set(["ecc-init", "ecc-breakup"])
/** Breakup is the way out of an error, so an error does not lock it. */
const IN_ERROR_OK = new Set(["breakup", "ecc-breakup"])

function tone(state?: RccState): Tone {
  if (!state) return "idle"
  if (state.error || state.code === 5) return "alarm"
  if (state.transition || state.code === 7 || state.code === 6) return "warn"
  return state.code === RUNNING || state.code === READY ? "ok" : "idle"
}

/**
 * The run control's state, and -- for a controller -- its five commands.
 * A command the state does not allow is drawn but cannot be pressed; the
 * server refuses it too, so the gate holds even for a stale page.
 */
export function RunControl({
  rcc,
  control,
  processes,
  container,
  onOpenConfig,
}: {
  rcc: Rcc | null
  control: boolean
  /** The DAQ's processes as the host sees them, drawn under the equipment. */
  processes: { label: string; on: boolean; tone: Tone; title: string }[]
  container: string
  /** Opens the Configuration tab. */
  onOpenConfig: () => void
}) {
  const [pending, setPending] = useState<string | null>(null)
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null)
  /** The next run's note, sent with Start -- what the run-control GUI asks for. */
  const [note, setNote] = useState("")
  const state = rcc?.reachable ? rcc.state : undefined

  const send = async (verb: string) => {
    setPending(verb)
    try {
      const answer = await fetch(`api/rcc/${verb}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(verb === "start" ? { note } : {}),
      })
      const data = await answer.json().catch(() => ({}))
      if (verb === "start" && data.ok) setNote("")
      setSaid(answer.ok ? { ok: !!data.ok, text: `${verb}: ${data.reply}` } : { ok: false, text: `${verb}: ${data.detail ?? answer.statusText}` })
    } catch (err) {
      setSaid({ ok: false, text: `${verb}: ${String(err)}` })
    } finally {
      setPending(null)
    }
  }
  const ecc = rcc?.equipment?.find((e) => e.type === "eccore" || e.type === "ecc")
  const allowed = (verb: string) => {
    if (!state || state.transition || (state.error && !IN_ERROR_OK.has(verb))) return false
    if (ECC_ONLY.has(verb)) return !!ecc && state.code !== RUNNING && state.code !== PAUSED && ALLOWED[verb].includes(ecc.state.code)
    return ALLOWED[verb].includes(state.code)
  }
  const key = (verb: string, label: string, extra: { primary?: boolean; danger?: boolean } = {}) => (
    <Command small {...extra} disabled={!!pending || !allowed(verb)} onRun={() => void send(verb)}>
      {pending === verb ? "…" : label}
    </Command>
  )

  return (
    <Panel
      title={
        <span className="gn-tags">
          Run control
          <Tag label="STATE" on={state?.code === RUNNING}>
            <Hold reserve={["NOMONITORING", "—"]}>{state ? state.name : "—"}</Hold>
          </Tag>
          <Tag label="RUN">{rcc?.run_number ?? "—"}</Tag>
          <Tag label="STORAGE" on={rcc?.storage === "on"}>
            <Hold reserve={["mixed"]}>{rcc?.storage ?? "—"}</Hold>
          </Tag>
        </span>
      }
      actions={
        <StateDot tone={tone(state)} reserve={["unreachable", "in transition", "NOMONITORING"]}>
          {!rcc?.reachable ? "unreachable" : state?.transition ? "in transition" : state?.name}
        </StateDot>
      }
    >
      {/* Eight keys of one size, in pairs: setting up on the first row (all, then the ECC alone),
          taking data on the second (storage, then the run). */}
      {control ? (
        <div className="gn-keys">
          <span className="gn-pair">
            {key("init", "Init")}
            {key("breakup", "Breakup")}
          </span>
          <span className="gn-pair">
            {key("ecc-init", "ECC init")}
            {key("ecc-breakup", "ECC breakup")}
          </span>
          <span className="gn-break" />
          <span className="gn-pair">
            {key("storage-on", "Storage on")}
            {key("storage-off", "Storage off")}
          </span>
          <span className="gn-pair">
            {key("start", "Start", { primary: true })}
            {key("stop", "Stop", { danger: true })}
          </span>
        </div>
      ) : null}
      {/* The configuration loaded; the Configuration tab lays it out. */}
      <button type="button" className="gn-config" title="Open in the Configuration tab" onClick={onOpenConfig} disabled={!rcc?.config_file}>
        <span className="faint">configuration </span>
        {rcc?.config_file ? rcc.config_file.split("/").pop() : "—"}
        <span className="faint">{rcc?.config_file ? `  ${rcc.config_file.slice(0, rcc.config_file.lastIndexOf("/") + 1)}` : ""}</span>
      </button>
      <div className="gn-stack gn-tight">
        {/* One line always (rule 5): unreachable, the last command's answer, or what the state allows. */}
        <NoticeLine tone={!rcc?.reachable ? "alarm" : said && !said.ok ? "warn" : null}>
          {!rcc?.reachable
            ? rcc?.error ?? "run control not asked yet"
            : said
              ? said.text
              : `${rcc.experiment ?? ""} · monitoring ${rcc.monitoring ? "on" : "off"}${state?.transition ? " · in transition" : ""}${state?.error ? ` · error ${state.error}` : ""}`}
        </NoticeLine>
        {control ? (
          <input
            className="neon-input gn-note-input"
            placeholder="Note for the next run — sent with Start, kept with the run"
            value={note}
            maxLength={2000}
            onChange={(e) => setNote(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && allowed("start") && !pending) void send("start")
            }}
          />
        ) : null}
        {/* A storage actor that is not writing is yellow, in words too: a run
            taken with storage off keeps nothing, and that is worth seeing. */}
        <ul className="neon-flags">
          {(rcc?.equipment ?? []).map((item) => {
            const off = item.type === "storage" && !item.storage_on
            const live = item.state.code !== 0
            return (
              <li
                key={item.name}
                title={`${item.type} · ${item.state.name}${item.type === "storage" ? ` · storage ${item.storage_on ? "on" : "off"}` : ""}`}
                className={off ? "gn-sun" : live ? `tone-text-${tone(item.state)}` : "faint"}
              >
                <span className={`neon-flag-mark${live ? " on" : ""}`} />
                {item.type === "storage" ? `${item.name} · ${item.storage_on ? "rec" : "off"}` : item.name}
              </li>
            )
          })}
        </ul>
        <div className="gn-sub" title={`Processes in ${container}, read from the host's /proc`}>
          Processes in {container}
        </div>
        <Flags items={processes} />
      </div>
    </Panel>
  )
}
