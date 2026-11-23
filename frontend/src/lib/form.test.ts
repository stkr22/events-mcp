import { describe, expect, it } from 'vitest';
import {
  applyTypePreset, emptySourceForm, formFromConfig, formFromDefaults, toDefaultsPayload, toSourcePayload,
  validateSourceForm,
} from './form';

describe('toSourcePayload', () => {
  it('omits every unset optional field', () => {
    const f = { ...emptySourceForm('venue'), name: 'museum-x', url: ' https://x.test/ ' };
    expect(toSourcePayload(f)).toEqual({ name: 'museum-x', type: 'venue', url: 'https://x.test/' });
  });

  it('sends channel_id and never url for youtube', () => {
    const f = { ...emptySourceForm('youtube'), name: 'chan', channelId: 'UCabc', url: 'https://stale.test' };
    expect(toSourcePayload(f)).toEqual({ name: 'chan', type: 'youtube', channel_id: 'UCabc', interval: '6h' });
  });

  it('drops blank list entries and keeps the rest', () => {
    const f = { ...emptySourceForm(), name: 'a', url: 'https://a.test', urls: ['https://a.test/2', '  '], egress: [''] };
    const p = toSourcePayload(f);
    expect(p.urls).toEqual(['https://a.test/2']);
    expect('egress' in p).toBe(false);
  });

  it('pins rps to the default when only ignore_robots is set', () => {
    const f = { ...emptySourceForm(), name: 'a', url: 'https://a.test', ignoreRobots: true };
    expect(toSourcePayload(f, { defaultRps: 0.5 }).politeness).toEqual({ rps: 0.5, ignore_robots: true });
    expect(toSourcePayload({ ...f, ignoreRobots: false }).politeness).toBeUndefined();
  });

  it('builds a venue only when something is filled in and converts coordinates', () => {
    const f = { ...emptySourceForm(), name: 'a', url: 'https://a.test' };
    expect(toSourcePayload(f).venue).toBeUndefined();
    const g = { ...f, venue: { name: 'Hall', city: '', address: '', lat: '52.5', lon: '13.4' } };
    expect(toSourcePayload(g).venue).toEqual({ name: 'Hall', lat: 52.5, lon: 13.4 });
  });
});

describe('formFromConfig', () => {
  it('round-trips a stored config through the form', () => {
    const config = {
      name: 'n', type: 'venue', url: 'https://n.test', urls: [], max_pages: 3, interval: '12h', city: null,
      politeness: { rps: 2, ignore_robots: true }, intent: 'only concerts', timeout_sec: 60, nav_timeout_sec: 90,
      venue: { name: 'V', city: 'Berlin', address: null, lat: 1, lon: 2 }, egress: ['n.test'], render: 'browser',
    };
    const payload = toSourcePayload(formFromConfig(config));
    expect(payload).toMatchObject({
      name: 'n', max_pages: 3, interval: '12h', timeout_sec: 60, nav_timeout_sec: 90, render: 'browser', intent: 'only concerts',
      politeness: { rps: 2, ignore_robots: true }, venue: { name: 'V', city: 'Berlin', lat: 1, lon: 2 }, egress: ['n.test'],
    });
    expect('city' in payload).toBe(false);
  });

  it('tolerates a sparse config', () => {
    expect(formFromConfig({}).type).toBe('venue');
  });
});

describe('type presets', () => {
  it('sets 6h for youtube and undoes it when switching away untouched', () => {
    const y = applyTypePreset(emptySourceForm('venue'), 'youtube');
    expect(y.interval).toBe('6h');
    expect(applyTypePreset(y, 'museum').interval).toBe('');
  });
  it('keeps an interval the admin typed', () => {
    expect(applyTypePreset({ ...emptySourceForm('venue'), interval: '2d' }, 'youtube').interval).toBe('2d');
  });
});

describe('validateSourceForm', () => {
  it('flags non-numbers, bad intervals and a nameless venue', () => {
    const f = {
      ...emptySourceForm(), name: 'a', maxPages: 'x', interval: '5 minutes', rps: '1.5x',
      venue: { name: '', city: 'Berlin', address: '', lat: '', lon: '' },
    };
    expect(Object.keys(validateSourceForm(f, true)).sort()).toEqual(['interval', 'max_pages', 'politeness.rps', 'venue.name']);
  });
  it('requires a name only when creating', () => {
    expect(validateSourceForm(emptySourceForm(), true).name).toBeDefined();
    expect(validateSourceForm(emptySourceForm(), false).name).toBeUndefined();
  });
});

describe('defaults mapping', () => {
  it('maps defaults both ways', () => {
    const d = { city: null, timezone: 'Europe/Berlin', interval: '24h', render: 'http', extractor: 'auto', politeness: { rps: 1, ignore_robots: false } } as const;
    const form = formFromDefaults(d);
    expect(form.city).toBe('');
    const { city: _city, ...rest } = d;
    expect(toDefaultsPayload(form)).toEqual(rest);
  });
});
