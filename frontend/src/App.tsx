import { useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ExecutionTrace } from './types';
import { Header } from './components/layout/Header';
import { NudgeGenerationPage } from './pages/NudgeGenerationPage';
import { AdminPage } from './pages/AdminPage';
import { ResultsViewerPage } from './pages/ResultsViewerPage';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30000,
      retry: 1,
    },
  },
});

function App() {
  const [activeTab, setActiveTab] = useState<'nudges' | 'admin' | 'results'>('nudges');
  const [latestTrace, setLatestTrace] = useState<ExecutionTrace | null>(null);

  const handleTraceAvailable = (trace: ExecutionTrace | null) => {
    if (trace) {
      setLatestTrace(trace);
    }
  };

  return (
    <QueryClientProvider client={queryClient}>
      <div className="min-h-screen">
        <Header activeTab={activeTab} onTabChange={setActiveTab} />
        <main>
          <div className={activeTab === 'nudges' ? '' : 'hidden'}>
            <NudgeGenerationPage onTraceAvailable={handleTraceAvailable} />
          </div>
          <div className={activeTab === 'admin' ? '' : 'hidden'}>
            <AdminPage latestTrace={latestTrace} />
          </div>
          <div className={activeTab === 'results' ? '' : 'hidden'}>
            <ResultsViewerPage />
          </div>
        </main>
      </div>
    </QueryClientProvider>
  );
}

export default App;
