const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 365 * 86400],
  ['month', 30 * 86400],
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
];

/** "3 hours ago", "in 2 days", "never" for a missing value. */
export function relativeTime(iso: string | null | undefined, now: number = Date.now(), locale?: string): string {
  if (!iso) return 'never';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const diff = Math.round((t - now) / 1000);
  const abs = Math.abs(diff);
  if (abs < 45) return 'just now';
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
  for (const [unit, secs] of UNITS) {
    if (abs >= secs) return rtf.format(Math.trunc(diff / secs), unit);
  }
  return rtf.format(Math.trunc(diff / 60), 'minute');
}

/** The browser-locale absolute time used in `title` attributes. */
export function absoluteTime(iso: string | null | undefined, locale?: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'medium' });
}

const INTERVAL = /^(\d+)\s*([mhd])$/i;

export function isValidInterval(value: string): boolean {
  const m = INTERVAL.exec(value.trim());
  return m !== null && Number(m[1]) >= 1;
}

/** "120m" becomes "2 h", "48h" "2 d"; anything that is not an interval is returned as it came. */
export function formatInterval(value: string | null | undefined): string {
  if (!value) return '';
  const m = INTERVAL.exec(value.trim());
  if (!m) return value;
  let n = Number(m[1]);
  let unit = (m[2] ?? '').toLowerCase();
  if (unit === 'm' && n % 60 === 0) [n, unit] = [n / 60, 'h'];
  if (unit === 'h' && n % 24 === 0) [n, unit] = [n / 24, 'd'];
  return `${n} ${unit === 'm' ? 'min' : unit}`;
}

/** "45s", "1m 12s", "2h 3m"; "running" while unfinished. */
export function formatDuration(start: string, end: string | null | undefined): string {
  if (!end) return 'running';
  const secs = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 1000));
  if (Number.isNaN(secs)) return '';
  if (secs < 60) return `${secs}s`;
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m ${secs % 60}s`;
}
