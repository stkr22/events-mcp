import { ApiError } from '../api/client';

export interface MappedErrors {
  /** Messages keyed by dotted API field path, e.g. `venue.name` or `urls.0`. */
  fields: Record<string, string>;
  /** Anything that does not name a field of this form. */
  banner: string | null;
}

const clean = (msg: string) => msg.replace(/^Value error,\s*/i, '');

/** Splits a FastAPI error into per-field messages and a banner. `known` holds the form's top-level field names. */
export function mapApiError(err: unknown, known: ReadonlySet<string>): MappedErrors {
  if (!(err instanceof ApiError)) {
    return { fields: {}, banner: err instanceof Error ? err.message : 'unexpected error' };
  }
  if (!Array.isArray(err.detail)) return { fields: {}, banner: err.message };
  const fields: Record<string, string> = {};
  const banner: string[] = [];
  for (const item of err.detail as unknown[]) {
    if (typeof item !== 'object' || item === null) continue;
    const { loc, msg } = item as { loc?: unknown; msg?: unknown };
    const text = clean(String(msg ?? 'invalid'));
    const path = (Array.isArray(loc) ? loc : []).map(String);
    if (path[0] === 'body') path.shift();
    const top = path[0];
    if (top !== undefined && known.has(top)) {
      const key = path.join('.');
      fields[key] = fields[key] ? `${fields[key]}; ${text}` : text;
    } else {
      banner.push(path.length ? `${path.join('.')}: ${text}` : text);
    }
  }
  return { fields, banner: banner.length ? banner.join('; ') : null };
}

/** The message for a field or, for lists, for any of its items. */
export function fieldError(fields: Record<string, string>, name: string): string | undefined {
  if (fields[name]) return fields[name];
  const prefix = `${name}.`;
  const hit = Object.keys(fields).find((k) => k.startsWith(prefix));
  return hit ? `${hit.slice(prefix.length)}: ${fields[hit]}` : undefined;
}
