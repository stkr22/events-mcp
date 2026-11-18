import type { components } from './schema';

type S = components['schemas'];

export type Source = S['Source'];
export type Defaults = S['Defaults'];
export type Politeness = S['Politeness'];
export type Venue = S['Venue'];
export type SourceView = S['SourceView'];
export type SourceDetail = S['SourceDetail'];
export type RunView = S['RunView'];
export type HistoryEntry = S['HistoryEntry'];
export type Effective = S['Effective'];
export type Me = S['Me'];
export type Options = S['Options'];
export type ValidationItem = S['ValidationError'];

export type SourceType = Source['type'];
export type Render = NonNullable<Source['render']>;
export type ExtractorName = NonNullable<Source['extractor']>;
export type KindName = NonNullable<Source['default_kind']>;
export type RunMode = S['RunRequest']['mode'];

/** What the UI sends: the schema marks defaulted fields required, but the backend accepts them missing. */
type Defaulted = 'urls' | 'egress' | 'ignore_patterns' | 'max_pages' | 'max_llm_calls' | 'max_input_chars';
export type SourceBody = Omit<Source, Defaulted> & Partial<Pick<Source, Defaulted>>;
export type DefaultsBody = Omit<Defaults, 'politeness'> & { politeness: Politeness };

export const FALLBACK_OPTIONS: Options = {
  source_types: ['venue', 'museum', 'youtube', 'feed', 'shop'],
  renders: ['http', 'browser', 'browser_stealth'],
  extractors: ['auto', 'ical', 'rss', 'youtube', 'jsonld', 'selectors', 'llm'],
  kinds: ['event', 'exhibition', 'video', 'sale', 'other'],
  run_modes: ['due', 'full'],
};
