import { useQueryClient } from '@tanstack/react-query';
import { api } from './api/endpoints';
import { useMe } from './api/hooks';
import type { Me } from './api/types';
import { Button, ConfirmDialog, Link, LoadError, Loading, ToastProvider, useToast } from './components/ui';
import { DefaultsPage } from './pages/DefaultsPage';
import { HistoryPage } from './pages/HistoryPage';
import { NewSourcePage, SourceEditPage } from './pages/SourcePage';
import { SourcesPage } from './pages/SourcesPage';
import { cancelPending, navigateNow, useRoute, usePendingNavigation, type Route } from './lib/route';

function Centered({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-4 px-4 py-10">{children}</main>;
}

function SignIn({ me }: { me: Me }) {
  return (
    <Centered>
      <h1 className="text-xl font-semibold">events-mcp admin</h1>
      <p className="text-dim">Sign in to manage the crawler configuration.</p>
      <div>
        {/* Full navigation: the server starts the OIDC redirect. */}
        <a href="/auth/login" className="inline-block rounded-md bg-accent px-4 py-2 text-sm font-medium text-onaccent hover:opacity-90">Sign in</a>
      </div>
      {!me.oidc && (
        <p className="text-sm text-dim">Single sign-on is not configured on this server. For local development use <a className="text-accent underline" href="/auth/dev-login">the development sign-in</a>.</p>
      )}
    </Centered>
  );
}

function NotAuthorised({ me }: { me: Me }) {
  return (
    <Centered>
      <h1 className="text-xl font-semibold">Not authorised</h1>
      <p>{me.name ?? me.email ?? 'This account'} is signed in but is not allowed to edit the crawler configuration.</p>
      <p className="text-sm text-dim">
        Editing needs the role <code className="font-semibold">{me.required_role}</code> in Zitadel. Ask an administrator to grant it, then sign out and in again.
      </p>
      <p className="text-sm text-dim" data-testid="roles">
        Roles received for this session: {me.roles && me.roles.length > 0 ? me.roles.join(', ') : 'none'}
      </p>
      <div><SignOut /></div>
    </Centered>
  );
}

function SignOut() {
  const toast = useToast();
  return (
    <Button onClick={() => api.logout().then(() => window.location.reload(), (e: Error) => toast('bad', e.message))}>Sign out</Button>
  );
}

function Nav({ me, route }: { me: Me; route: Route }) {
  const item = (href: string, label: string, on: boolean) => (
    <Link href={href} className={`rounded-md px-2.5 py-1.5 text-sm hover:no-underline ${on ? 'bg-subtle font-medium text-fg' : 'text-dim hover:bg-subtle'}`}>
      <span {...(on ? { 'aria-current': 'page' as const } : {})}>{label}</span>
    </Link>
  );
  return (
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2">
        <span className="font-semibold">events-mcp admin</span>
        <nav aria-label="Main" className="flex flex-wrap gap-1">
          {item('#/', 'Sources', route.page === 'sources' || route.page === 'source' || route.page === 'new')}
          {item('#/defaults', 'Defaults', route.page === 'defaults')}
          {item('#/history', 'History', route.page === 'history')}
        </nav>
        <span className="ml-auto flex items-center gap-2 text-sm text-dim">
          <span className="max-w-[14rem] truncate" title={me.sub ?? ''}>{me.name ?? me.email ?? me.sub}</span>
          <SignOut />
        </span>
      </div>
    </header>
  );
}

function Shell({ me }: { me: Me }) {
  const route = useRoute();
  const pending = usePendingNavigation();
  return (
    <>
      <Nav me={me} route={route} />
      <main className="mx-auto max-w-6xl px-4 py-5">
        {route.page === 'sources' && <SourcesPage />}
        {route.page === 'source' && <SourceEditPage name={route.name} />}
        {route.page === 'new' && <NewSourcePage />}
        {route.page === 'defaults' && <DefaultsPage />}
        {route.page === 'history' && <HistoryPage />}
      </main>
      <ConfirmDialog open={pending !== null} title="Discard unsaved changes?" confirmLabel="Discard and leave" cancelLabel="Keep editing" danger
        onCancel={cancelPending} onConfirm={() => pending && navigateNow(pending)}>
        <p>This form has edits that were not saved.</p>
      </ConfirmDialog>
    </>
  );
}

function Gate() {
  const me = useMe();
  const qc = useQueryClient();
  if (me.isPending) return <Centered><Loading what="session" /></Centered>;
  if (me.isError) return <Centered><LoadError error={me.error} onRetry={() => void qc.invalidateQueries({ queryKey: ['me'] })} /></Centered>;
  if (!me.data.authenticated) return <SignIn me={me.data} />;
  if (!me.data.admin) return <NotAuthorised me={me.data} />;
  return <Shell me={me.data} />;
}

export default function App() {
  return (
    <ToastProvider>
      <Gate />
    </ToastProvider>
  );
}

