import { useState } from 'react';
import { CheckCircle, AlertCircle, XCircle, HelpCircle, MessageSquare } from 'lucide-react';

interface AnnotationPanelProps {
  sampleId: string;
  nudgeIndex?: number;
  onSaved?: () => void;
}

const CATEGORIES = [
  { value: 'looks_good', label: 'Looks Good', icon: CheckCircle, color: 'text-green-500' },
  { value: 'hallucination', label: 'Hallucination', icon: XCircle, color: 'text-red-500' },
  { value: 'wrong_citation', label: 'Wrong Citation', icon: AlertCircle, color: 'text-orange-500' },
  { value: 'missing_fields', label: 'Missing Fields', icon: HelpCircle, color: 'text-yellow-500' },
  { value: 'medical_error', label: 'Medical Error', icon: XCircle, color: 'text-red-600' },
  { value: 'other', label: 'Other', icon: MessageSquare, color: 'text-gray-500' },
];

export function AnnotationPanel({ sampleId, nudgeIndex, onSaved }: AnnotationPanelProps) {
  const [annotation, setAnnotation] = useState('');
  const [category, setCategory] = useState('looks_good');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    setSaving(true);
    setError(null);

    try {
      const response = await fetch('/api/eval/annotations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sample_id: sampleId,
          nudge_index: nudgeIndex,
          annotation,
          category,
        }),
      });

      if (!response.ok) {
        throw new Error('Failed to save annotation');
      }

      setSaved(true);
      onSaved?.();

      // Reset after short delay
      setTimeout(() => {
        setSaved(false);
        setAnnotation('');
      }, 2000);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="glass-card p-4 space-y-4">
      <h4 className="font-display text-sm text-[--color-text-primary] flex items-center gap-2">
        <MessageSquare className="w-4 h-4" />
        Annotation
      </h4>

      {/* Category Selection */}
      <div className="grid grid-cols-3 gap-2">
        {CATEGORIES.map(({ value, label, icon: Icon, color }) => (
          <button
            key={value}
            onClick={() => setCategory(value)}
            className={`p-2 rounded-lg text-xs font-medium flex items-center gap-1.5 transition-all ${
              category === value
                ? 'bg-[--color-accent-primary]/20 border-2 border-[--color-accent-primary]'
                : 'bg-[--color-bg-tertiary] border-2 border-transparent hover:border-[--color-border]'
            }`}
          >
            <Icon className={`w-3.5 h-3.5 ${color}`} />
            {label}
          </button>
        ))}
      </div>

      {/* Notes */}
      <textarea
        value={annotation}
        onChange={(e) => setAnnotation(e.target.value)}
        placeholder="Add notes (optional)..."
        className="w-full p-3 text-sm bg-[--color-bg-secondary] border border-[--color-border] rounded-lg text-[--color-text-primary] placeholder:text-[--color-text-muted] focus:outline-none focus:ring-2 focus:ring-[--color-accent-primary]/50 resize-none"
        rows={2}
      />

      {/* Error display */}
      {error && (
        <div className="text-sm text-red-500 flex items-center gap-1">
          <XCircle className="w-4 h-4" />
          {error}
        </div>
      )}

      {/* Save Button */}
      <button
        onClick={handleSave}
        disabled={saving || saved}
        className={`w-full py-2 px-4 rounded-lg font-medium text-sm transition-all ${
          saved
            ? 'bg-green-500 text-white'
            : 'bg-[--color-accent-primary] text-white hover:bg-[--color-accent-primary]/90'
        } disabled:opacity-50`}
      >
        {saving ? 'Saving...' : saved ? '✓ Saved' : 'Save Annotation'}
      </button>
    </div>
  );
}
