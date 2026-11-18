import type { Options } from '../api/types';
import { fieldError } from '../lib/errors';
import type { SourceForm } from '../lib/form';
import { Card, CheckboxField, ListEditor, SelectField, TextAreaField, TextField } from './ui';

/** What an empty field falls back to; shown as placeholder text. */
export interface Inherited {
  interval: string;
  city: string;
  timezone: string;
  render: string;
  extractor: string;
  kind: string;
  rps: string;
}

interface Props {
  form: SourceForm;
  patch: (p: Partial<SourceForm>) => void;
  setType: (t: SourceForm['type']) => void;
  errors: Record<string, string>;
  options: Options;
  inherited: Inherited;
  creating: boolean;
}

const grid = 'grid gap-4 sm:grid-cols-2';

export function SourceFields({ form, patch, setType, errors, options, inherited, creating }: Props) {
  const err = (k: string) => fieldError(errors, k);
  const yt = form.type === 'youtube';
  const inherit = (v: string) => `inherit (${v || 'default'})`;
  return (
    <div className="space-y-4">
      <Card title="Identity">
        <div className={grid}>
          <TextField label="Name" value={form.name} onChange={(v) => patch({ name: v })} disabled={!creating} mono
            hint={creating ? 'Lowercase slug (a-z, 0-9, -). It cannot be changed later.' : 'Fixed after creation.'} error={err('name')} />
          <SelectField label="Type" value={form.type} onChange={(v) => setType(v as SourceForm['type'])} options={options.source_types} error={err('type')} />
        </div>
      </Card>

      <Card title="Where">
        <div className="space-y-4">
          {yt ? (
            <TextField label="Channel ID" value={form.channelId} onChange={(v) => patch({ channelId: v })} placeholder="UC…" mono
              hint="The channel's UC… id. Required for youtube." error={err('channel_id')} />
          ) : (
            <TextField label="URL" value={form.url} onChange={(v) => patch({ url: v })} placeholder="https://…" mono error={err('url')} />
          )}
          <ListEditor label="Additional URLs" items={form.urls} onChange={(urls) => patch({ urls })} placeholder="https://…" addLabel="Add URL"
            hint="More listing pages, fetched after the first, up to Max pages in total." error={err('urls')} />
          <div className="max-w-xs">
            <TextField label="Max pages" value={form.maxPages} onChange={(v) => patch({ maxPages: v })} placeholder="1" inputMode="numeric"
              hint="1 to 20 listing pages." error={err('max_pages')} />
          </div>
        </div>
      </Card>

      <Card title="Fetching">
        <div className="space-y-4">
          <div className={grid}>
            <SelectField label="Render" value={form.render} onChange={(v) => patch({ render: v as SourceForm['render'] })} options={options.renders}
              blankLabel={inherit(inherited.render)} error={err('render')} />
            <TextField label="Interval" value={form.interval} onChange={(v) => patch({ interval: v })} placeholder={inherited.interval} mono
              hint="How often to re-crawl: 30m, 6h or 2d." error={err('interval')} />
            <TextField label="Timeout (seconds)" value={form.timeoutSec} onChange={(v) => patch({ timeoutSec: v })} placeholder="300" inputMode="numeric"
              hint="Hard limit for one run, 30 to 3000. Empty uses the crawler-wide 300 s." error={err('timeout_sec')} />
            <TextField label="Wait for selector" value={form.waitForSelector} onChange={(v) => patch({ waitForSelector: v })} placeholder="CSS selector" mono
              hint="Browser rendering waits for this element before reading the page." error={err('wait_for_selector')} />
            <TextField label="Requests per second (per host)" value={form.rps} onChange={(v) => patch({ rps: v })} placeholder={inherited.rps} inputMode="decimal"
              hint="Above 0, at most 10." error={err('politeness.rps') ?? err('politeness')} />
          </div>
          <CheckboxField label="Ignore robots.txt" checked={form.ignoreRobots} onChange={(v) => patch({ ignoreRobots: v })}
            hint={yt ? 'Offered for YouTube feeds that robots.txt disallows.' : 'Off means robots.txt is respected.'}
            warn="Ignoring robots.txt overrides a site's stated wishes. This is an owner decision: only enable it for a source you have decided to crawl anyway, and note why." />
        </div>
      </Card>

      <Card title="Extraction">
        <div className="space-y-4">
          <div className={grid}>
            <SelectField label="Extractor" value={form.extractor} onChange={(v) => patch({ extractor: v as SourceForm['extractor'] })} options={options.extractors}
              blankLabel={inherit(inherited.extractor)} error={err('extractor')} />
            <SelectField label="Default kind" value={form.defaultKind} onChange={(v) => patch({ defaultKind: v as SourceForm['defaultKind'] })} options={options.kinds}
              blankLabel={inherit(inherited.kind)} hint="Kind assigned when the page does not say." error={err('default_kind')} />
            <TextField label="Max LLM calls per run" value={form.maxLlmCalls} onChange={(v) => patch({ maxLlmCalls: v })} placeholder="10" inputMode="numeric" error={err('max_llm_calls')} />
            <TextField label="Max input characters" value={form.maxInputChars} onChange={(v) => patch({ maxInputChars: v })} placeholder="150000" inputMode="numeric" error={err('max_input_chars')} />
          </div>
          <TextAreaField label="Intent" value={form.intent} onChange={(v) => patch({ intent: v })} rows={6}
            placeholder="What counts as an event here, and what to ignore."
            hint="Instructions for the LLM extractor. After editing, use Re-extract now so unchanged pages are read again." error={err('intent')} />
          <ListEditor label="Ignore patterns" items={form.ignorePatterns} onChange={(ignorePatterns) => patch({ ignorePatterns })} addLabel="Add pattern"
            hint="Text or URL patterns the extractor should skip." error={err('ignore_patterns')} />
        </div>
      </Card>

      <Card title="Place and time">
        <div className="space-y-4">
          <div className={grid}>
            <TextField label="City" value={form.city} onChange={(v) => patch({ city: v })} placeholder={inherited.city} error={err('city')} />
            <TextField label="Timezone" value={form.timezone} onChange={(v) => patch({ timezone: v })} placeholder={inherited.timezone} mono error={err('timezone')} />
          </div>
          <fieldset className="space-y-3 rounded-md border border-line p-3">
            <legend className="px-1 text-sm font-medium">Fixed venue (optional)</legend>
            <p className="text-xs text-dim">For single-venue sources: skips geocoding. Leave all empty to geocode per event.</p>
            <div className={grid}>
              <TextField label="Venue name" value={form.venue.name} onChange={(v) => patch({ venue: { ...form.venue, name: v } })} error={err('venue.name')} />
              <TextField label="Venue city" value={form.venue.city} onChange={(v) => patch({ venue: { ...form.venue, city: v } })} error={err('venue.city')} />
              <TextField label="Address" value={form.venue.address} onChange={(v) => patch({ venue: { ...form.venue, address: v } })} error={err('venue.address')} />
              <div className="grid grid-cols-2 gap-4">
                <TextField label="Latitude" value={form.venue.lat} onChange={(v) => patch({ venue: { ...form.venue, lat: v } })} inputMode="decimal" hint="-90 to 90" error={err('venue.lat')} />
                <TextField label="Longitude" value={form.venue.lon} onChange={(v) => patch({ venue: { ...form.venue, lon: v } })} inputMode="decimal" hint="-180 to 180" error={err('venue.lon')} />
              </div>
            </div>
            {err('venue') && <p className="text-xs text-bad">{err('venue')}</p>}
          </fieldset>
        </div>
      </Card>

      <Card title="Network">
        <ListEditor label="Egress hosts" items={form.egress} onChange={(egress) => patch({ egress })} placeholder="example.org" addLabel="Add host"
          hint="Informational: it documents which hosts this source may reach and is not enforced by the crawler. Empty means the hosts of the URLs." error={err('egress')} />
      </Card>
    </div>
  );
}
