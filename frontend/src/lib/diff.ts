export interface Change {
  key: string;
  before: unknown;
  after: unknown;
}

function canon(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canon).join(',')}]`;
  if (typeof v === 'object' && v !== null) {
    const o = v as Record<string, unknown>;
    return `{${Object.keys(o)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canon(o[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(v) ?? 'undefined';
}

/** Keys whose value differs; a missing key and an explicit null count as the same "unset". */
export function changedKeys(before: Record<string, unknown> | null, after: Record<string, unknown> | null): Change[] {
  const b = before ?? {};
  const a = after ?? {};
  const keys = [...new Set([...Object.keys(b), ...Object.keys(a)])].sort();
  const out: Change[] = [];
  for (const key of keys) {
    const bv = b[key] ?? null;
    const av = a[key] ?? null;
    if (canon(bv) !== canon(av)) out.push({ key, before: bv, after: av });
  }
  return out;
}

export function show(v: unknown): string {
  if (v === null || v === undefined) return '(unset)';
  return typeof v === 'string' ? v : JSON.stringify(v);
}
