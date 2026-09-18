import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { FileText, Copy, Check, ChevronDown } from 'lucide-react';
import { fetchPrompts, fetchPromptContent } from '../../api/client';
import { Card } from '../common/Card';

export function PromptViewer() {
  const [selectedPrompt, setSelectedPrompt] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const { data: promptsData } = useQuery({
    queryKey: ['prompts'],
    queryFn: fetchPrompts,
  });

  const { data: promptContent, isLoading: isLoadingContent } = useQuery({
    queryKey: ['prompt', selectedPrompt],
    queryFn: () => fetchPromptContent(selectedPrompt!),
    enabled: !!selectedPrompt,
  });

  const prompts = promptsData?.prompts ?? [];

  const handleCopy = async () => {
    if (promptContent?.content) {
      await navigator.clipboard.writeText(promptContent.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <Card title="Prompt Viewer" subtitle="View system prompts and specialty instructions">
      {/* Prompt Selector */}
      <div className="mb-4">
        <div className="relative">
          <select
            value={selectedPrompt || ''}
            onChange={(e) => setSelectedPrompt(e.target.value || null)}
            className="w-full px-4 py-3 bg-[--color-bg-tertiary] border border-[--color-border] rounded-lg text-[--color-text-primary] focus:outline-none focus:border-[--color-accent-primary] transition-colors appearance-none cursor-pointer"
          >
            <option value="">Select a prompt...</option>
            <optgroup label="Core Prompts">
              {prompts
                .filter((p) => p.category === 'core')
                .map((prompt) => (
                  <option key={prompt.name} value={prompt.name}>
                    {prompt.name}
                  </option>
                ))}
            </optgroup>
            <optgroup label="Specialty Instructions">
              {prompts
                .filter((p) => p.category === 'specialty')
                .map((prompt) => (
                  <option key={prompt.name} value={prompt.name}>
                    {prompt.name}
                  </option>
                ))}
            </optgroup>
          </select>
          <ChevronDown className="absolute right-4 top-1/2 -translate-y-1/2 w-4 h-4 text-[--color-text-muted] pointer-events-none" />
        </div>
      </div>

      {/* Prompt Content */}
      {selectedPrompt && (
        <div className="relative">
          {/* Header */}
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2 text-sm text-[--color-text-muted]">
              <FileText className="w-4 h-4" />
              <span className="font-mono">{promptContent?.path}</span>
            </div>
            <button
              onClick={handleCopy}
              className="flex items-center gap-2 px-3 py-1.5 bg-[--color-bg-tertiary] rounded-lg text-sm text-[--color-text-secondary] hover:text-[--color-text-primary] transition-colors"
            >
              {copied ? (
                <>
                  <Check className="w-4 h-4 text-[--color-success]" />
                  Copied!
                </>
              ) : (
                <>
                  <Copy className="w-4 h-4" />
                  Copy
                </>
              )}
            </button>
          </div>

          {/* Content */}
          <div className="bg-[--color-bg-tertiary] rounded-lg border border-[--color-border] overflow-hidden">
            {isLoadingContent ? (
              <div className="p-6 flex items-center justify-center">
                <div className="w-6 h-6 border-2 border-[--color-accent-primary] border-t-transparent rounded-full animate-spin" />
              </div>
            ) : (
              <pre className="p-4 overflow-x-auto text-sm text-[--color-text-secondary] font-mono leading-relaxed max-h-[500px] overflow-y-auto">
                {promptContent?.content}
              </pre>
            )}
          </div>
        </div>
      )}

      {!selectedPrompt && (
        <div className="py-12 text-center">
          <FileText className="w-12 h-12 text-[--color-text-muted] mx-auto mb-3 opacity-50" />
          <p className="text-[--color-text-muted]">Select a prompt to view its content</p>
        </div>
      )}
    </Card>
  );
}
