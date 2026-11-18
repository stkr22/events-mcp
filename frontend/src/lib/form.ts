import type {
  Defaults,
  DefaultsBody,
  ExtractorName,
  KindName,
  Render,
  SourceBody,
  SourceType,
  Venue,
} from '../api/types';
import { isValidInterval } from './format';

/** Every field is text so an empty input means "unset" (omitted from the payload, inherits the default). */
export interface VenueForm {
  name: string;
  city: string;
  address: string;
  lat: string;
  lon: string;
}

export interface SourceForm {
  name: string;
  type: SourceType;
  url: string;
  urls: string[];
  channelId: string;
  maxPages: string;
  interval: string;
  timeoutSec: string;
  render: '' | Render;
  waitForSelector: string;
  rps: string;
  ignoreRobots: boolean;
  extractor: '' | ExtractorName;
  defaultKind: '' | KindName;
  intent: string;
  ignorePatterns: string[];
  maxLlmCalls: string;
  maxInputChars: string;
  city: string;
  timezone: string;
  venue: VenueForm;
  egress: string[];
}

/** Top-level API field names this form covers; used to route errors to a field or the banner. */
export const SOURCE_FIELDS: ReadonlySet<string> = new Set([
  'name', 'type', 'url', 'urls', 'channel_id', 'max_pages', 'interval', 'city', 'timezone', 'render',
  'extractor', 'politeness', 'default_kind', 'intent', 'venue', 'egress', 'ignore_patterns',
  'wait_for_selector', 'max_llm_calls', 'max_input_chars', 'timeout_sec',
]);

export const DEFAULTS_FIELDS: ReadonlySet<string> = new Set([
  'city', 'timezone', 'interval', 'render', 'extractor', 'politeness',
]);

const EMPTY_VENUE: VenueForm = { name: '', city: '', address: '', lat: '', lon: '' };

export function emptySourceForm(type: SourceType = 'venue'): SourceForm {
  return applyTypePreset(
    {
      name: '', type, url: '', urls: [], channelId: '', maxPages: '', interval: '', timeoutSec: '',
      render: '', waitForSelector: '', rps: '', ignoreRobots: false, extractor: '', defaultKind: '',
      intent: '', ignorePatterns: [], maxLlmCalls: '', maxInputChars: '', city: '', timezone: '',
      venue: { ...EMPTY_VENUE }, egress: [],
    },
    type,
  );
}

/** Per-type starting values for a new source: youtube polls every 6 h; others inherit the default interval. */
export function applyTypePreset(form: SourceForm, type: SourceType): SourceForm {
  const next = { ...form, type };
  if (type === 'youtube' && form.interval === '') next.interval = '6h';
  if (type !== 'youtube' && form.type === 'youtube' && form.interval === '6h') next.interval = '';
  return next;
}

// --- reading the stored config -------------------------------------------------------------------
const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const num = (v: unknown): string => (typeof v === 'number' ? String(v) : '');
const list = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);
const obj = (v: unknown): Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {};

/** Source form from the `config` object the API returns (the stored Source; nulls and gaps mean unset). */
export function formFromConfig(config: Record<string, unknown>): SourceForm {
  const pol = obj(config.politeness);
  const venue = obj(config.venue);
  return {
    name: str(config.name),
    type: (str(config.type) || 'venue') as SourceType,
    url: str(config.url),
    urls: list(config.urls),
    channelId: str(config.channel_id),
    maxPages: num(config.max_pages),
    interval: str(config.interval),
    timeoutSec: num(config.timeout_sec),
    render: str(config.render) as SourceForm['render'],
    waitForSelector: str(config.wait_for_selector),
    rps: num(pol.rps),
    ignoreRobots: pol.ignore_robots === true,
    extractor: str(config.extractor) as SourceForm['extractor'],
    defaultKind: str(config.default_kind) as SourceForm['defaultKind'],
    intent: str(config.intent),
    ignorePatterns: list(config.ignore_patterns),
    maxLlmCalls: num(config.max_llm_calls),
    maxInputChars: num(config.max_input_chars),
    city: str(config.city),
    timezone: str(config.timezone),
    venue: { name: str(venue.name), city: str(venue.city), address: str(venue.address), lat: num(venue.lat), lon: num(venue.lon) },
    egress: list(config.egress),
  };
}

// --- building the payload --------------------------------------------------------------------------
const t = (s: string): string | undefined => (s.trim() === '' ? undefined : s.trim());
const n = (s: string): number | undefined => (s.trim() === '' ? undefined : Number(s));
const items = (xs: string[]): string[] => xs.map((x) => x.trim()).filter(Boolean);

export interface PayloadOptions {
  /** The defaults' rps. A source `politeness` object replaces the default one wholesale, so when only
   *  ignore_robots is set the rps is pinned to this value instead of silently falling back to 1.0. */
  defaultRps?: number;
}

function set<T extends object, K extends keyof T>(o: T, key: K, v: T[K] | undefined): void {
  if (v !== undefined) o[key] = v;
}

/** The PUT/POST body: unset optional fields are left out so the backend inherits the default. */
export function toSourcePayload(f: SourceForm, opts: PayloadOptions = {}): SourceBody {
  const body: SourceBody = { name: f.name.trim(), type: f.type };
  if (f.type === 'youtube') set(body, 'channel_id', t(f.channelId));
  else set(body, 'url', t(f.url));
  const urls = items(f.urls);
  if (urls.length) body.urls = urls;
  set(body, 'max_pages', n(f.maxPages));
  set(body, 'interval', t(f.interval));
  set(body, 'timeout_sec', n(f.timeoutSec));
  set(body, 'render', f.render || undefined);
  set(body, 'wait_for_selector', t(f.waitForSelector));
  const rps = n(f.rps);
  if (rps !== undefined || f.ignoreRobots) {
    body.politeness = { rps: rps ?? opts.defaultRps ?? 1, ignore_robots: f.ignoreRobots };
  }
  set(body, 'extractor', f.extractor || undefined);
  set(body, 'default_kind', f.defaultKind || undefined);
  set(body, 'intent', t(f.intent));
  const patterns = items(f.ignorePatterns);
  if (patterns.length) body.ignore_patterns = patterns;
  set(body, 'max_llm_calls', n(f.maxLlmCalls));
  set(body, 'max_input_chars', n(f.maxInputChars));
  set(body, 'city', t(f.city));
  set(body, 'timezone', t(f.timezone));
  const v = f.venue;
  if (Object.values(v).some((x) => x.trim() !== '')) {
    const venue: Venue = { name: v.name.trim() };
    set(venue, 'city', t(v.city));
    set(venue, 'address', t(v.address));
    set(venue, 'lat', n(v.lat));
    set(venue, 'lon', n(v.lon));
    body.venue = venue;
  }
  const egress = items(f.egress);
  if (egress.length) body.egress = egress;
  return body;
}

const NUMERIC: [keyof SourceForm, string, boolean][] = [
  ['maxPages', 'max_pages', true],
  ['timeoutSec', 'timeout_sec', true],
  ['rps', 'politeness.rps', false],
  ['maxLlmCalls', 'max_llm_calls', true],
  ['maxInputChars', 'max_input_chars', true],
];

/** Mistakes the browser can see without asking the backend (blank name, non-numbers, half a venue). Keyed like API errors. */
export function validateSourceForm(f: SourceForm, creating: boolean): Record<string, string> {
  const e: Record<string, string> = {};
  if (creating && f.name.trim() === '') e.name = 'name is required';
  for (const [key, field, integer] of NUMERIC) {
    const raw = String(f[key]).trim();
    if (raw === '') continue;
    const v = Number(raw);
    if (!Number.isFinite(v)) e[field] = 'must be a number';
    else if (integer && !Number.isInteger(v)) e[field] = 'must be a whole number';
  }
  for (const k of ['lat', 'lon'] as const) {
    const raw = f.venue[k].trim();
    if (raw !== '' && !Number.isFinite(Number(raw))) e[`venue.${k}`] = 'must be a number';
  }
  const v = f.venue;
  if (Object.values(v).some((x) => x.trim() !== '') && v.name.trim() === '') e['venue.name'] = 'a venue needs a name';
  if (f.interval.trim() !== '' && !isValidInterval(f.interval)) e.interval = 'use a form like 30m, 6h or 2d';
  return e;
}

export const isDirty = <T>(a: T, b: T): boolean => JSON.stringify(a) !== JSON.stringify(b);

// --- defaults ---------------------------------------------------------------------------------------
export interface DefaultsForm {
  city: string;
  timezone: string;
  interval: string;
  render: Render;
  extractor: ExtractorName;
  rps: string;
  ignoreRobots: boolean;
}

export function formFromDefaults(d: Defaults): DefaultsForm {
  return {
    city: d.city ?? '',
    timezone: d.timezone,
    interval: d.interval,
    render: d.render,
    extractor: d.extractor,
    rps: String(d.politeness.rps),
    ignoreRobots: d.politeness.ignore_robots,
  };
}

export function toDefaultsPayload(f: DefaultsForm): DefaultsBody {
  const body: DefaultsBody = {
    timezone: f.timezone.trim(),
    interval: f.interval.trim(),
    render: f.render,
    extractor: f.extractor,
    politeness: { rps: Number(f.rps), ignore_robots: f.ignoreRobots },
  };
  set(body, 'city', t(f.city));
  return body;
}

export function validateDefaultsForm(f: DefaultsForm): Record<string, string> {
  const e: Record<string, string> = {};
  if (!isValidInterval(f.interval)) e.interval = 'use a form like 30m, 6h or 2d';
  if (f.timezone.trim() === '') e.timezone = 'timezone is required';
  if (f.rps.trim() === '' || !Number.isFinite(Number(f.rps))) e['politeness.rps'] = 'must be a number';
  return e;
}
