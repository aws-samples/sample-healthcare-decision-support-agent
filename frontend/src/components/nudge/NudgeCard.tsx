import { useState } from 'react';
import { ChevronDown, BookOpen, FileCode, Clipboard } from 'lucide-react';
import type { Nudge } from '../../types';
import { Badge } from '../common/Badge';

interface NudgeCardProps {
  nudge: Nudge;
  index: number;
}

const urgencyVariants: Record<string, 'urgent' | 'warning' | 'info'> = {
  urgent: 'urgent',
  warning: 'warning',
  informational: 'info',
};

export function NudgeCard({ nudge, index }: NudgeCardProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  const urgencyVariant = urgencyVariants[nudge.urgency] || 'info';

  // Determine left border color based on urgency
  const urgencyBorderColor =
    nudge.urgency === 'urgent' ? 'border-l-[--color-urgent]' :
    nudge.urgency === 'warning' ? 'border-l-[--color-warning]' :
    'border-l-[--color-info]';

  return (
    <div className={`bg-[--color-bg-secondary] rounded-xl border border-[--color-border] border-l-4 ${urgencyBorderColor} overflow-hidden transition-all hover:shadow-md`}>
      {/* Header */}
      <div className="p-4">
        <div className="flex items-start gap-3">
          {/* Index Badge */}
          <span className="flex items-center justify-center w-7 h-7 rounded-lg bg-[--color-bg-tertiary] text-sm font-semibold text-[--color-text-secondary] flex-shrink-0">
            {index + 1}
          </span>

          <div className="flex-1 min-w-0">
            {/* Tags Row */}
            <div className="flex items-center gap-2 mb-2 flex-wrap">
              <Badge variant={urgencyVariant} size="sm">
                {nudge.urgency.charAt(0).toUpperCase() + nudge.urgency.slice(1)}
              </Badge>
              <span className="px-2 py-0.5 rounded text-xs font-medium border bg-violet-100 dark:bg-violet-500/20 text-violet-700 dark:text-violet-400 border-violet-200 dark:border-violet-500/30">
                {nudge.category.replace(/_/g, ' ')}
              </span>
              <span className="px-2 py-0.5 rounded text-xs font-medium border bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-500/30">
                {nudge.nudge_type.replace(/_/g, ' ')}
              </span>
              <span className="px-2 py-0.5 rounded text-xs font-medium border bg-gray-100 dark:bg-gray-500/20 text-gray-700 dark:text-gray-400 border-gray-200 dark:border-gray-500/30">
                {nudge.action_type.replace(/_/g, ' ')}
              </span>
            </div>

            {/* Title */}
            <h3 className="font-display text-base text-[--color-text-primary] leading-snug">
              {nudge.title}
            </h3>

            {/* Description */}
            <p className="mt-1.5 text-sm text-[--color-text-secondary] leading-relaxed">
              {nudge.description}
            </p>
          </div>
        </div>
      </div>

      {/* Expandable Details Toggle */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full px-4 py-2.5 border-t border-[--color-border] flex items-center justify-between text-sm text-[--color-text-muted] hover:bg-[--color-bg-tertiary]/50 transition-colors"
      >
        <span className="font-medium">{isExpanded ? 'Hide details' : 'Show details'}</span>
        <ChevronDown className={`w-4 h-4 transition-transform duration-200 ${isExpanded ? 'rotate-180' : ''}`} />
      </button>

      {/* Expanded Content */}
      <div className={`overflow-hidden transition-all duration-200 ${isExpanded ? 'max-h-[600px] opacity-100' : 'max-h-0 opacity-0'}`}>
        <div className="px-4 pb-4 space-y-4 border-t border-[--color-border] pt-4 bg-[--color-bg-tertiary]/30">
          {/* Rationale */}
          <div>
            <h4 className="flex items-center gap-2 text-sm font-semibold text-[--color-text-primary] mb-2">
              <Clipboard className="w-4 h-4 text-[--color-text-muted]" />
              Rationale
            </h4>
            <p className="text-sm text-[--color-text-secondary] leading-relaxed bg-[--color-bg-secondary] p-3 rounded-lg border border-[--color-border]">
              {nudge.rationale}
            </p>
          </div>

          {/* Guideline Citation */}
          {nudge.guideline_citation && (
            <div>
              <h4 className="flex items-center gap-2 text-sm font-semibold text-[--color-text-primary] mb-2">
                <BookOpen className="w-4 h-4 text-[--color-text-muted]" />
                Guideline Citation
              </h4>
              <div className="bg-[--color-bg-secondary] p-3 rounded-lg border border-[--color-border]">
                <p className="text-sm text-[--color-accent-primary] font-medium">
                  {nudge.guideline_citation.source}
                </p>
                {nudge.guideline_citation.section && (
                  <p className="text-sm text-[--color-text-secondary] mt-1">
                    {nudge.guideline_citation.section}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Codes */}
          {(nudge.icd_codes.length > 0 || nudge.cpt_codes.length > 0) && (
            <div>
              <h4 className="flex items-center gap-2 text-sm font-semibold text-[--color-text-primary] mb-1">
                <FileCode className="w-4 h-4 text-[--color-text-muted]" />
                Clinical Codes*
              </h4>
              <p className="text-xs text-[--color-text-muted] italic mb-2">
                *AI-generated codes, not clinically validated
              </p>
              <div className="flex flex-wrap gap-2">
                {nudge.icd_codes.map((code) => (
                  <span
                    key={code}
                    className="px-2 py-1 bg-violet-100 dark:bg-violet-500/20 text-violet-700 dark:text-violet-400 rounded text-xs font-mono border border-violet-200 dark:border-violet-500/30"
                  >
                    ICD: {code}
                  </span>
                ))}
                {nudge.cpt_codes.map((code) => (
                  <span
                    key={code}
                    className="px-2 py-1 bg-cyan-100 dark:bg-cyan-500/20 text-cyan-700 dark:text-cyan-400 rounded text-xs font-mono border border-cyan-200 dark:border-cyan-500/30"
                  >
                    CPT: {code}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Grounding */}
          <div className="flex items-center gap-2 text-xs text-[--color-text-muted] pt-2 border-t border-[--color-border]">
            <span>Grounding:</span>
            <span className="px-2 py-0.5 bg-[--color-bg-secondary] border border-[--color-border] rounded font-medium">
              {nudge.grounding}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
