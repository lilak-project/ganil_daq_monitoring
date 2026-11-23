/* The run catalog's shapes, and how its numbers are written. Shared by the Run and History tabs. */

export type DataFile = {
  name: string
  run: number
  started: string
  part: number
  size: number
  modified: string
  /** Relative to the run directory: `cobo0/run_0240.dat…`. */
  path: string
  /** Made from the raw files afterwards (a merge), not written by the DAQ. */
  derived: boolean
}

export type Attempt = {
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

/** Bytes as an operator reads them. Binary units, because that is what `ls -lh`
    on the run directory says and the two must agree. */
export function humanSize(bytes: number): string {
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
export function humanDuration(seconds: number | null): string {
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
export function localTime(iso: string | null): string {
  if (!iso) return "—"
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? "—" : when.toLocaleString()
}
