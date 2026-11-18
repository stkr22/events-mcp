import { describe, expect, it } from 'vitest';
import { formatDuration, formatInterval, isValidInterval, relativeTime } from './format';

const NOW = Date.parse('2026-11-18T12:00:00Z');

describe('relativeTime', () => {
  it('handles past, future, recent and missing', () => {
    expect(relativeTime('2026-11-18T09:00:00Z', NOW, 'en')).toBe('3 hours ago');
    expect(relativeTime('2026-11-20T12:00:00Z', NOW, 'en')).toBe('in 2 days');
    expect(relativeTime('2026-11-18T11:59:50Z', NOW, 'en')).toBe('just now');
    expect(relativeTime(null, NOW, 'en')).toBe('never');
  });
});

describe('interval', () => {
  it('normalises to the largest whole unit', () => {
    expect(formatInterval('120m')).toBe('2 h');
    expect(formatInterval('48h')).toBe('2 d');
    expect(formatInterval('90m')).toBe('90 min');
    expect(formatInterval('weekly')).toBe('weekly');
  });
  it('validates like the backend', () => {
    expect(isValidInterval('6h')).toBe(true);
    expect(isValidInterval('0h')).toBe(false);
    expect(isValidInterval('6 hours')).toBe(false);
  });
});

describe('formatDuration', () => {
  it('formats seconds, minutes, hours and unfinished runs', () => {
    const s = '2026-11-18T10:00:00Z';
    expect(formatDuration(s, '2026-11-18T10:00:45Z')).toBe('45s');
    expect(formatDuration(s, '2026-11-18T10:01:12Z')).toBe('1m 12s');
    expect(formatDuration(s, '2026-11-18T12:03:00Z')).toBe('2h 3m');
    expect(formatDuration(s, null)).toBe('running');
  });
});
