import { useState } from "react"
import { Command, Flags, Hold, NoticeLine, Panel, StateDot, Tag, type Tone } from "@neon"

/* What /api/status carries under `rcc`: the run control's state, asked over SOAP. */
export type RccState = { code: number; name: string; transition: number; error: number }
export type Rcc = {
  reachable: boolean
  error: string | null
  at: number | null
  experiment?: string
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
  "storage-on": [READY],
  "storage-off": [READY],
  start: [READY],
  stop: [RUNNING, PAUSED],
}

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
}: {
  rcc: Rcc | null
  control: boolean
  /** The DAQ's processes as the host sees them, drawn under the equipment. */
  processes: { label: string; on: boolean; tone: Tone; title: string }[]
  container: string
}) {
  const [pending, setPending] = useState<string | null>(null)
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null)
  const state = rcc?.reachable ? rcc.state : undefined
  const settled = !!state && !state.transition && !state.error

  const send = async (verb: string) => {
    setPending(verb)
    try {
      const answer = await fetch(`api/rcc/${verb}`, { method: "POST" })
      const data = await answer.json().catch(() => ({}))
      setSaid(answer.ok ? { ok: !!data.ok, text: `${verb}: ${data.reply}` } : { ok: false, text: `${verb}: ${data.detail ?? answer.statusText}` })
    } catch (err) {
      setSaid({ ok: false, text: `${verb}: ${String(err)}` })
    } finally {
      setPending(null)
    }
  }
  const key = (verb: string, label: string, extra: { primary?: boolean; danger?: boolean } = {}) => (
    <Command small {...extra} disabled={!!pending || !settled || !ALLOWED[verb].includes(state!.code)} onRun={() => void send(verb)}>
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
        control ? (
          <span className="gn-tags">
            {key("init", "Init")}
            {key("storage-on", "Storage on")}
            {key("storage-off", "Storage off")}
            {key("start", "Start", { primary: true })}
            {key("stop", "Stop", { danger: true })}
          </span>
        ) : (
          <StateDot tone={tone(state)} reserve={["unreachable", "in transition", "NOMONITORING"]}>
            {!rcc?.reachable ? "unreachable" : state?.transition ? "in transition" : state?.name}
          </StateDot>
        )
      }
    >
      <div className="gn-stack gn-tight">
        {/* One line always (rule 5): unreachable, the last command's answer, or what the state allows. */}
        <NoticeLine tone={!rcc?.reachable ? "alarm" : said && !said.ok ? "warn" : null}>
          {!rcc?.reachable
            ? rcc?.error ?? "run control not asked yet"
            : said
              ? said.text
              : `${rcc.experiment ?? ""} · monitoring ${rcc.monitoring ? "on" : "off"}${state?.transition ? " · in transition" : ""}${state?.error ? ` · error ${state.error}` : ""}`}
        </NoticeLine>
        <Flags
          items={(rcc?.equipment ?? []).map((item) => ({
            label: item.type === "storage" ? `${item.name} ${item.storage_on ? "●" : "○"}` : item.name,
            on: item.state.code !== 0,
            tone: tone(item.state),
            title: `${item.type} · ${item.state.name}${item.type === "storage" ? ` · storage ${item.storage_on ? "on" : "off"}` : ""}`,
          }))}
        />
        <div className="gn-sub" title={`Processes in ${container}, read from the host's /proc`}>
          Processes in {container}
        </div>
        <Flags items={processes} />
      </div>
    </Panel>
  )
}
