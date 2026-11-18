import {
  createContext, useCallback, useContext, useEffect, useId, useRef, useState,
  type ButtonHTMLAttributes, type MouseEvent, type ReactNode,
} from 'react';
import { go, isDirtyNow } from '../lib/route';

const cx = (...c: (string | false | undefined)[]) => c.filter(Boolean).join(' ');

// --- buttons and links -------------------------------------------------------------------------------
type Variant = 'primary' | 'secondary' | 'danger' | 'ghost';
const VARIANTS: Record<Variant, string> = {
  primary: 'bg-accent text-onaccent border-transparent hover:opacity-90',
  secondary: 'bg-surface text-fg border-line hover:bg-subtle',
  danger: 'bg-surface text-bad border-bad hover:bg-badbg',
  ghost: 'bg-transparent text-fg border-transparent hover:bg-subtle',
};

export function Button({ variant = 'secondary', className, type = 'button', ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={type}
      className={cx('inline-flex items-center justify-center gap-1 rounded-md border px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50', VARIANTS[variant], className)}
      {...rest}
    />
  );
}

/** An anchor to a hash route that asks before discarding unsaved edits. */
export function Link({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (isDirtyNow() && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      e.preventDefault();
      go(href);
    }
  };
  return (
    <a href={href} onClick={onClick} className={cx('text-accent hover:underline', className)}>
      {children}
    </a>
  );
}

// --- badges ---------------------------------------------------------------------------------------------
export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral';
const TONES: Record<Tone, string> = {
  ok: 'bg-okbg text-ok',
  warn: 'bg-warnbg text-warn',
  bad: 'bg-badbg text-bad',
  info: 'bg-infobg text-info',
  neutral: 'bg-subtle text-dim',
};

export function Badge({ tone = 'neutral', title, children }: { tone?: Tone; title?: string; children: ReactNode }) {
  return (
    <span title={title} className={cx('inline-block whitespace-nowrap rounded px-1.5 py-0.5 text-xs font-medium', TONES[tone])}>
      {children}
    </span>
  );
}

const STATUS_TONE: Record<string, Tone> = {
  success: 'ok', partial: 'warn', failed: 'bad', pending_extraction: 'info', blocked_robots: 'warn', running: 'info', skipped: 'neutral',
};

export function StatusBadge({ status, error }: { status: string | null | undefined; error?: string | null }) {
  if (!status) return <Badge title="no run recorded yet">never run</Badge>;
  const label = status.replace(/_/g, ' ');
  return (
    <Badge tone={STATUS_TONE[status] ?? 'neutral'} title={error ? `${label}: ${error}` : label}>
      <span className="sr-only">Last run status: </span>
      {label}
    </Badge>
  );
}

// --- layout -----------------------------------------------------------------------------------------------
export function Card({ title, children, actions, className }: { title?: ReactNode; children: ReactNode; actions?: ReactNode; className?: string }) {
  return (
    <section className={cx('rounded-lg border border-line bg-surface', className)}>
      {(title || actions) && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5">
          <h2 className="text-sm font-semibold">{title}</h2>
          {actions}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Banner({ tone = 'bad', children }: { tone?: 'bad' | 'warn' | 'info'; children: ReactNode }) {
  return (
    <div role={tone === 'bad' ? 'alert' : 'status'} className={cx('rounded-md px-3 py-2 text-sm', TONES[tone])}>
      {children}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <p role="status" className="py-8 text-center text-dim">Loading {what}…</p>;
}

export function LoadError({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div className="space-y-2 py-6">
      <Banner>Could not load: {error.message}</Banner>
      {onRetry && <Button onClick={onRetry}>Retry</Button>}
    </div>
  );
}

// --- form fields -----------------------------------------------------------------------------------------------
interface FieldMeta { label: string; hint?: ReactNode; error?: string | undefined }

function FieldShell({ id, label, hint, error, children }: FieldMeta & { id: string; children: ReactNode }) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium">{label}</label>
      {children}
      {hint && <p id={`${id}-hint`} className="text-xs text-dim">{hint}</p>}
      {error && <p id={`${id}-err`} className="text-xs text-bad">{error}</p>}
    </div>
  );
}

const inputCls = (error?: string) =>
  cx('block w-full rounded-md border bg-surface px-2.5 py-1.5 text-sm disabled:bg-subtle disabled:text-dim', error ? 'border-bad' : 'border-line');

function describedBy(id: string, m: FieldMeta) {
  return { 'aria-describedby': [m.hint && `${id}-hint`, m.error && `${id}-err`].filter(Boolean).join(' ') || undefined, 'aria-invalid': m.error ? true : undefined };
}

export function TextField({ label, hint, error, value, onChange, placeholder, disabled, mono, inputMode }: FieldMeta & {
  value: string; onChange: (v: string) => void; placeholder?: string; disabled?: boolean; mono?: boolean; inputMode?: 'numeric' | 'decimal' | 'text';
}) {
  const id = useId();
  return (
    <FieldShell id={id} label={label} hint={hint} error={error}>
      <input id={id} type="text" value={value} disabled={disabled} inputMode={inputMode} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)} className={cx(inputCls(error), mono && 'font-mono')} {...describedBy(id, { label, hint, error })} />
    </FieldShell>
  );
}

export function TextAreaField({ label, hint, error, value, onChange, placeholder, rows = 6 }: FieldMeta & {
  value: string; onChange: (v: string) => void; placeholder?: string; rows?: number;
}) {
  const id = useId();
  return (
    <FieldShell id={id} label={label} hint={hint} error={error}>
      <textarea id={id} value={value} rows={rows} placeholder={placeholder} onChange={(e) => onChange(e.target.value)}
        className={inputCls(error)} {...describedBy(id, { label, hint, error })} />
    </FieldShell>
  );
}

export function SelectField({ label, hint, error, value, onChange, options, blankLabel, disabled }: FieldMeta & {
  value: string; onChange: (v: string) => void; options: readonly string[]; blankLabel?: string; disabled?: boolean;
}) {
  const id = useId();
  return (
    <FieldShell id={id} label={label} hint={hint} error={error}>
      <select id={id} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} className={inputCls(error)} {...describedBy(id, { label, hint, error })}>
        {blankLabel !== undefined && <option value="">{blankLabel}</option>}
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </FieldShell>
  );
}

export function CheckboxField({ label, hint, checked, onChange, warn }: { label: string; hint?: ReactNode; checked: boolean; onChange: (v: boolean) => void; warn?: ReactNode }) {
  const id = useId();
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <input id={id} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="size-4 accent-[var(--accent)]" aria-describedby={hint ? `${id}-hint` : undefined} />
        <label htmlFor={id} className="text-sm font-medium">{label}</label>
      </div>
      {hint && <p id={`${id}-hint`} className="text-xs text-dim">{hint}</p>}
      {checked && warn && <Banner tone="warn">{warn}</Banner>}
    </div>
  );
}

/** A list of text rows with add and remove; an empty list is simply "unset". */
export function ListEditor({ label, hint, error, items, onChange, placeholder, addLabel = 'Add' }: FieldMeta & {
  items: string[]; onChange: (v: string[]) => void; placeholder?: string; addLabel?: string;
}) {
  const id = useId();
  return (
    <fieldset className="space-y-1.5" aria-describedby={[hint && `${id}-hint`, error && `${id}-err`].filter(Boolean).join(' ') || undefined}>
      <legend className="text-sm font-medium">{label}</legend>
      {items.map((v, i) => (
        <div key={i} className="flex gap-2">
          <input aria-label={`${label} ${i + 1}`} type="text" value={v} placeholder={placeholder}
            onChange={(e) => onChange(items.map((x, j) => (j === i ? e.target.value : x)))} className={cx(inputCls(error), 'font-mono')} />
          <Button variant="ghost" aria-label={`Remove ${label} ${i + 1}`} onClick={() => onChange(items.filter((_, j) => j !== i))}>Remove</Button>
        </div>
      ))}
      <div><Button onClick={() => onChange([...items, ''])}>{addLabel}</Button></div>
      {hint && <p id={`${id}-hint`} className="text-xs text-dim">{hint}</p>}
      {error && <p id={`${id}-err`} className="text-xs text-bad">{error}</p>}
    </fieldset>
  );
}

// --- dialog ------------------------------------------------------------------------------------------------------
/** Native <dialog>: focus is trapped, Escape cancels, and it is announced as a modal. */
export function ConfirmDialog({ open, title, children, confirmLabel, cancelLabel = 'Cancel', danger, busy, onConfirm, onCancel }: {
  open: boolean; title: string; children: ReactNode; confirmLabel: string; cancelLabel?: string; danger?: boolean; busy?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} aria-labelledby={titleId} onCancel={(e) => { e.preventDefault(); onCancel(); }}
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded-lg border border-line bg-surface p-5 shadow-xl">
      <h2 id={titleId} className="text-base font-semibold">{title}</h2>
      <div className="mt-2 space-y-2 text-sm">{children}</div>
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <Button onClick={onCancel}>{cancelLabel}</Button>
        <Button variant={danger ? 'danger' : 'primary'} disabled={busy} onClick={onConfirm}>{confirmLabel}</Button>
      </div>
    </dialog>
  );
}

// --- toasts ---------------------------------------------------------------------------------------------------------
interface Toast { id: number; tone: 'ok' | 'bad'; text: string }
const ToastCtx = createContext<(tone: Toast['tone'], text: string) => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((tone: Toast['tone'], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, tone, text }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === 'bad' ? 9000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div aria-live="polite" className="fixed inset-x-4 bottom-4 z-50 flex flex-col items-end gap-2">
        {toasts.map((t) => (
          <div key={t.id} role={t.tone === 'bad' ? 'alert' : 'status'}
            className={cx('flex max-w-md items-start gap-3 rounded-md border px-3 py-2 text-sm shadow-lg', t.tone === 'ok' ? 'border-ok bg-okbg text-ok' : 'border-bad bg-badbg text-bad')}>
            <span>{t.text}</span>
            <button type="button" aria-label="Dismiss" className="font-bold" onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}>×</button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
