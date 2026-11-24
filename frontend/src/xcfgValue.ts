/* CompoundConfig's value and range rules, for checking an edit as it is typed.
   The server checks again before saving (xcfg.py: value_of, parse_range,
   in_range -- the same rules, verified against the library). */

type Kind = "int" | "hex" | "real" | "bool" | "string"
type Typed = [Kind, number | boolean | string]

const REAL = /^[+-]?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$/
const HEX = /^0x([0-9a-fA-F]+)$/
const INT = /^[+-]?\d+$/

export function valueOf(text: string): Typed {
  const t = text.trim()
  if (REAL.test(t)) return ["real", parseFloat(t)]
  const h = HEX.exec(t)
  if (h && parseInt(h[1], 16) <= 0xffffffff) return ["hex", parseInt(h[1], 16)]
  if (INT.test(t) && Number(t) >= -(2 ** 31) && Number(t) <= 2 ** 31 - 1) return ["int", Number(t)]
  if (t.toLowerCase() === "true" || t.toLowerCase() === "false") return ["bool", t.toLowerCase() === "true"]
  return ["string", t]
}

const unquote = (s: string) => (s.length >= 2 && s[0] === '"' && s[s.length - 1] === '"' ? s.slice(1, -1) : s)

export type Range =
  | { kind: "interval"; lower: Typed | null; upper: Typed | null; lowerMode: string; upperMode: string }
  | { kind: "enum"; values: Typed[]; raw: string[] }

export function parseRange(text: string | null | undefined): Range | null {
  if (!text) return null
  const t = text.trim()
  let m = /^([[\]])\s*([^,[\]]*?)\s*,\s*([^,[\]]*?)\s*([[\]])$/.exec(t)
  if (m) {
    const [lo, hi] = [m[2], m[3]]
    return {
      kind: "interval",
      lowerMode: lo === "-inf" ? "unbounded" : m[1] === "[" ? "include" : "exclude",
      upperMode: hi === "+inf" ? "unbounded" : m[4] === "]" ? "include" : "exclude",
      lower: lo === "-inf" ? null : valueOf(unquote(lo)),
      upper: hi === "+inf" ? null : valueOf(unquote(hi)),
    }
  }
  m = /^\{\s*(.*?)\s*\}$/.exec(t)
  if (m) {
    const raw = m[1] ? m[1].split(/\s*,\s*/).filter(Boolean).map(unquote) : []
    return { kind: "enum", values: raw.map(valueOf), raw }
  }
  return null
}

export function inRange(text: string, range: Range | null): boolean {
  if (!range) return true
  const [kind, value] = valueOf(text)
  if (range.kind === "enum") return range.values.some(([k, v]) => k === kind && v === value)
  if (range.lowerMode !== "unbounded") {
    const [k, lo] = range.lower!
    if (k !== kind || !(range.lowerMode === "include" ? lo <= value : lo < value)) return false
  }
  if (range.upperMode !== "unbounded") {
    const [k, hi] = range.upper!
    if (k !== kind || !(range.upperMode === "include" ? value <= hi : value < hi)) return false
  }
  return true
}
