import { describe, expect, it } from 'vitest';
import { ApiError } from '../api/client';
import { fieldError, mapApiError } from './errors';
import { SOURCE_FIELDS } from './form';

const e422 = (detail: unknown) => new ApiError(422, detail, 'x');

describe('mapApiError', () => {
  it('maps a body error to its field and strips pydantic noise', () => {
    const r = mapApiError(e422([{ loc: ['body', 'name'], msg: 'Value error, name must be a lowercase slug' }]), SOURCE_FIELDS);
    expect(r.fields).toEqual({ name: 'name must be a lowercase slug' });
    expect(r.banner).toBeNull();
  });

  it('keeps nested paths and list indexes', () => {
    const r = mapApiError(
      e422([
        { loc: ['body', 'venue', 'lat'], msg: 'Input should be less than or equal to 90' },
        { loc: ['body', 'urls', 1], msg: 'not an http(s) URL' },
      ]),
      SOURCE_FIELDS,
    );
    expect(r.fields['venue.lat']).toBeDefined();
    expect(fieldError(r.fields, 'urls')).toBe('1: not an http(s) URL');
  });

  it('sends model-level and unknown-field errors to the banner', () => {
    const r = mapApiError(
      e422([
        { loc: ['body'], msg: 'Value error, url is required' },
        { loc: ['body', 'bogus'], msg: 'Extra inputs are not permitted' },
      ]),
      SOURCE_FIELDS,
    );
    expect(r.fields).toEqual({});
    expect(r.banner).toBe('url is required; bogus: Extra inputs are not permitted');
  });

  it('uses a string detail as the banner', () => {
    expect(mapApiError(new ApiError(409, "'a' already exists", "'a' already exists"), SOURCE_FIELDS).banner).toBe("'a' already exists");
  });
});
