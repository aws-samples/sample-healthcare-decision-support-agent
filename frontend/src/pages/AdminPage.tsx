import type { ExecutionTrace } from '../types';
import { PromptViewer } from '../components/admin/PromptViewer';
import { ExecutionTracePanel } from '../components/admin/ExecutionTracePanel';
import { Card } from '../components/common/Card';

interface AdminPageProps {
  latestTrace: ExecutionTrace | null;
}

export function AdminPage({ latestTrace }: AdminPageProps) {
  return (
    <div className="max-w-[1400px] mx-auto px-6 py-8">
      <div className="mb-8">
        <h2 className="font-display text-2xl text-[--color-text-primary]">Admin Panel</h2>
        <p className="text-[--color-text-muted] mt-1">
          View prompts, debug execution traces, and monitor system behavior
        </p>
      </div>

      <div className="grid grid-cols-12 gap-6">
        {/* Left Column - Prompt Viewer */}
        <div className="col-span-12 lg:col-span-5">
          <PromptViewer />
        </div>

        {/* Right Column - Execution Trace */}
        <div className="col-span-12 lg:col-span-7">
          {latestTrace ? (
            <ExecutionTracePanel trace={latestTrace} />
          ) : (
            <Card title="Execution Trace" subtitle="Debug the latest nudge generation">
              <div className="py-16 text-center">
                <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-[--color-bg-tertiary] flex items-center justify-center">
                  <svg
                    className="w-8 h-8 text-[--color-text-muted]"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={1.5}
                      d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
                    />
                  </svg>
                </div>
                <h3 className="font-display text-lg text-[--color-text-primary]">
                  No Trace Available
                </h3>
                <p className="text-[--color-text-muted] mt-2 max-w-sm mx-auto">
                  Generate nudges from the Nudge Generation tab to capture an execution trace for debugging.
                </p>
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
