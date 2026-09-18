import { useQuery } from '@tanstack/react-query';
import { Stethoscope, Building2, MessageSquare, Sparkles } from 'lucide-react';
import { fetchSpecialties } from '../../api/client';
import type { VisitContext } from '../../types';
import { Card } from '../common/Card';

interface VisitContextFormProps {
  value: VisitContext;
  onChange: (context: VisitContext) => void;
  onGenerate: () => void;
  isGenerating: boolean;
  disabled?: boolean;
}

export function VisitContextForm({
  value,
  onChange,
  onGenerate,
  isGenerating,
  disabled = false,
}: VisitContextFormProps) {
  const { data: specialties = ['general', 'cardiology', 'endocrinology'] } = useQuery({
    queryKey: ['specialties'],
    queryFn: fetchSpecialties,
  });

  return (
    <Card title="Visit Context" subtitle="Configure the clinical context for nudge generation">
      <div className="space-y-4">
        {/* Visit Type */}
        <div>
          <label className="block text-sm font-semibold text-[--color-text-primary] mb-2">
            <span className="flex items-center gap-2">
              <Building2 className="w-4 h-4 text-[--color-text-muted]" />
              Visit Type
            </span>
          </label>
          <div className="flex gap-2">
            {(['ambulatory', 'inpatient'] as const).map((type) => (
              <button
                key={type}
                onClick={() => onChange({ ...value, visit_type: type })}
                disabled={disabled}
                className={`flex-1 px-4 py-3 rounded-lg text-sm font-semibold transition-all border ${
                  value.visit_type === type
                    ? 'bg-[--color-text-primary] text-[--color-bg-secondary] border-[--color-text-primary]'
                    : 'bg-[--color-bg-secondary] text-[--color-text-secondary] border-[--color-border] hover:border-[--color-text-muted]'
                } disabled:opacity-50`}
              >
                {type.charAt(0).toUpperCase() + type.slice(1)}
              </button>
            ))}
          </div>
        </div>

        {/* Specialty */}
        <div>
          <label className="block text-sm font-semibold text-[--color-text-primary] mb-2">
            <span className="flex items-center gap-2">
              <Stethoscope className="w-4 h-4 text-[--color-text-muted]" />
              Specialty
            </span>
          </label>
          <select
            value={value.specialty}
            onChange={(e) => onChange({ ...value, specialty: e.target.value })}
            disabled={disabled}
            className="w-full px-4 py-3 bg-[--color-bg-secondary] border border-[--color-border] rounded-lg text-[--color-text-primary] focus:outline-none focus:border-[--color-text-primary] transition-colors disabled:opacity-50"
          >
            {specialties.map((specialty) => (
              <option key={specialty} value={specialty}>
                {specialty.charAt(0).toUpperCase() + specialty.slice(1)}
              </option>
            ))}
          </select>
        </div>

        {/* Chief Complaint */}
        <div>
          <label className="block text-sm font-semibold text-[--color-text-primary] mb-2">
            <span className="flex items-center gap-2">
              <MessageSquare className="w-4 h-4 text-[--color-text-muted]" />
              Chief Complaint
            </span>
          </label>
          <textarea
            value={value.chief_complaint || ''}
            onChange={(e) => onChange({ ...value, chief_complaint: e.target.value || null })}
            disabled={disabled}
            placeholder="e.g., Routine follow-up for hypertension"
            rows={2}
            className="w-full px-4 py-3 bg-[--color-bg-secondary] border border-[--color-border] rounded-lg text-[--color-text-primary] placeholder-[--color-text-muted] focus:outline-none focus:border-[--color-text-primary] transition-colors resize-none disabled:opacity-50"
          />
        </div>

        {/* Generate Button */}
        <button
          onClick={onGenerate}
          disabled={disabled || isGenerating}
          className="w-full py-4 rounded-xl font-semibold text-white transition-all bg-emerald-600 hover:bg-emerald-700 dark:bg-emerald-500 dark:hover:bg-emerald-600 shadow-md hover:shadow-lg disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <span className="flex items-center justify-center gap-2">
            {isGenerating ? (
              <>
                <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                Generating Nudges...
              </>
            ) : (
              <>
                <Sparkles className="w-5 h-5" />
                Generate Nudges
              </>
            )}
          </span>
        </button>
      </div>
    </Card>
  );
}
