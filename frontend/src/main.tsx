import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError } from './api/client';
import type { Me } from './api/types';
import App from './App';
import './index.css';

const onError = (error: unknown) => {
  // A lapsed session: drop back to the sign-in page without a reload.
  if (error instanceof ApiError && error.status === 401) {
    const prev = queryClient.getQueryData<Me>(['me']);
    queryClient.setQueryData<Me>(['me'], { authenticated: false, admin: false, oidc: prev?.oidc ?? true });
  }
};

const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({ onError }),
  mutationCache: new MutationCache({ onError }),
  defaultOptions: {
    queries: {
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

const root = document.getElementById('root');
if (!root) throw new Error('missing #root');
createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
