import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Route, Switch, Router as WouterRouter, useLocation } from 'wouter';
import { type ReactNode } from 'react';
import { ErrorBoundary } from '@/components/error-boundary';
import Dashboard from '@/pages/dashboard';
import NotFound from '@/pages/not-found';

/**
 * Polling is the only data strategy here: the bot is the source of truth and
 * the dashboard asks it for a snapshot. Retries are kept low so a dead bot
 * surfaces as OFFLINE quickly rather than spinning for a minute behind a
 * stale view.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 5_000,
      refetchOnWindowFocus: true,
    },
    mutations: { retry: 0 },
  },
});

function RoutedErrorBoundary({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  // Keying on the route clears a caught error when the user navigates.
  return <ErrorBoundary resetKey={location}>{children}</ErrorBoundary>;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
        <RoutedErrorBoundary>
          <Switch>
            <Route path="/" component={Dashboard} />
            <Route component={NotFound} />
          </Switch>
        </RoutedErrorBoundary>
      </WouterRouter>
    </QueryClientProvider>
  );
}
