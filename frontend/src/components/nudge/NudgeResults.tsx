import { Clock, CheckCircle, AlertCircle, FileText, Zap, AlertTriangle, Activity } from 'lucide-react';
import type { NudgeResponse } from '../../types';
import { Card } from '../common/Card';
import { Badge } from '../common/Badge';
import { NudgeCard } from './NudgeCard';

interface NudgeResultsProps {
  response: NudgeResponse;
}

export function NudgeResults({ response }: NudgeResultsProps) {
  const { status, nudges, metadata, warnings, error, patient_summary } = response;

  const urgentCount = nudges.filter((n) => n.urgency === 'urgent').length;
  const warningCount = nudges.filter((n) => n.urgency === 'warning').length;

  return (
    <div className="space-y-5">
      {/* Status Banner */}
      {status === 'error' && (
        <div className="p-4 rounded-xl bg-[--color-urgent-bg] border border-[--color-urgent]/20">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-[--color-urgent]/10 flex items-center justify-center">
              <AlertCircle className="w-5 h-5 text-[--color-urgent]" />
            </div>
            <div>
              <p className="font-semibold text-[--color-urgent]">Generation Failed</p>
              <p className="text-sm text-[--color-text-secondary] mt-0.5">{error}</p>
            </div>
          </div>
        </div>
      )}

      {warnings.length > 0 && (
        <div className="p-4 rounded-xl bg-[--color-warning-bg] border border-[--color-warning]/20">
          <div className="flex items-start gap-3">
            <div className="w-10 h-10 rounded-full bg-[--color-warning]/10 flex items-center justify-center flex-shrink-0">
              <AlertTriangle className="w-5 h-5 text-[--color-warning]" />
            </div>
            <div>
              <p className="font-semibold text-[--color-warning]">Warnings</p>
              <ul className="mt-1 space-y-1">
                {warnings.map((warning, i) => (
                  <li key={i} className="text-sm text-[--color-text-secondary]">
                    {warning}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* Clinical Nudges Section - Main Feature */}
      <div className="glass-card overflow-hidden">
        {/* Section Header with Integrated Stats */}
        <div className="px-5 py-4 bg-gradient-to-r from-[--color-accent-primary]/5 to-transparent border-b border-[--color-border]">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-[--color-accent-primary]/10 flex items-center justify-center">
                <Activity className="w-5 h-5 text-[--color-accent-primary]" />
              </div>
              <div>
                <h3 className="font-display text-xl text-[--color-text-primary]">
                  Clinical Nudges
                </h3>
                <p className="text-sm text-[--color-text-muted]">
                  Evidence-based recommendations
                </p>
              </div>
            </div>

            {/* Inline Stats */}
            <div className="flex items-center gap-2 flex-wrap">
              <StatPill
                label="Total"
                value={nudges.length}
                colorVar="--color-accent-primary"
                icon={<Zap className="w-3.5 h-3.5" />}
              />
              {urgentCount > 0 && (
                <StatPill
                  label="Urgent"
                  value={urgentCount}
                  colorVar="--color-urgent"
                  icon={<AlertCircle className="w-3.5 h-3.5" />}
                />
              )}
              {warningCount > 0 && (
                <StatPill
                  label="Warning"
                  value={warningCount}
                  colorVar="--color-warning"
                  icon={<AlertTriangle className="w-3.5 h-3.5" />}
                />
              )}
              <StatPill
                label=""
                value={`${(metadata.processing_time_ms / 1000).toFixed(1)}s`}
                colorVar="--color-text-muted"
                icon={<Clock className="w-3.5 h-3.5" />}
                subtle
              />
            </div>
          </div>
        </div>

        {/* Nudges List */}
        <div className="p-5 space-y-4">
          {nudges.length > 0 ? (
            nudges.map((nudge, index) => (
              <NudgeCard key={index} nudge={nudge} index={index} />
            ))
          ) : status !== 'error' ? (
            <div className="py-12 text-center">
              <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-[--color-success-bg] flex items-center justify-center">
                <CheckCircle className="w-8 h-8 text-[--color-success]" />
              </div>
              <p className="text-[--color-text-primary] font-semibold">No nudges generated</p>
              <p className="text-sm text-[--color-text-muted] mt-1 max-w-sm mx-auto">
                The patient appears to be up-to-date with their care plan based on current guidelines.
              </p>
            </div>
          ) : null}
        </div>

        {/* Footer Metadata */}
        {nudges.length > 0 && (
          <div className="px-5 py-3 bg-[--color-bg-tertiary]/50 border-t border-[--color-border] flex flex-wrap items-center gap-4 text-sm">
            <span className="flex items-center gap-2 text-[--color-text-muted]">
              <CheckCircle className="w-4 h-4" />
              <span className="text-[--color-text-secondary]">{metadata.model_version}</span>
            </span>
            {metadata.guidelines_used.length > 0 && (
              <span className="flex items-center gap-2 text-[--color-text-muted]">
                <FileText className="w-4 h-4" />
                <span className="flex items-center gap-1.5">
                  {metadata.guidelines_used.map((g) => (
                    <Badge key={g} variant="default" size="sm">
                      {g}
                    </Badge>
                  ))}
                </span>
              </span>
            )}
          </div>
        )}
      </div>

      {/* Patient Summary - Secondary */}
      {patient_summary && (
        <Card>
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-lg bg-[--color-info-bg] flex items-center justify-center flex-shrink-0">
              <FileText className="w-4 h-4 text-[--color-info]" />
            </div>
            <div className="flex-1">
              <h4 className="font-semibold text-[--color-text-primary] text-sm mb-1">Patient Summary</h4>
              <p className="text-sm text-[--color-text-secondary] leading-relaxed">{patient_summary}</p>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}

// Compact stat pill component for inline display
function StatPill({
  label,
  value,
  colorVar,
  icon,
  subtle = false
}: {
  label: string;
  value: string | number;
  colorVar: string;
  icon?: React.ReactNode;
  subtle?: boolean;
}) {
  return (
    <div
      className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm ${
        subtle
          ? 'bg-[--color-bg-tertiary] text-[--color-text-muted]'
          : ''
      }`}
      style={!subtle ? {
        backgroundColor: `color-mix(in srgb, var(${colorVar}) 10%, transparent)`,
        color: `var(${colorVar})`
      } : undefined}
    >
      {icon}
      <span className="font-semibold">{value}</span>
      {label && <span className={subtle ? '' : 'opacity-80'}>{label}</span>}
    </div>
  );
}
