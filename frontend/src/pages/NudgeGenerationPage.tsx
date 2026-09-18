import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { generateNudges } from '../api/client';
import type { PatientSummary, VisitContext, NudgeResponse } from '../types';
import { PatientSelector } from '../components/patient/PatientSelector';
import { PatientViewer } from '../components/patient/PatientViewer';
import { VisitContextForm } from '../components/nudge/VisitContextForm';
import { NudgeResults } from '../components/nudge/NudgeResults';
import { Card } from '../components/common/Card';

interface NudgeGenerationPageProps {
  onTraceAvailable: (trace: NudgeResponse['trace']) => void;
}

export function NudgeGenerationPage({ onTraceAvailable }: NudgeGenerationPageProps) {
  const [selectedPatient, setSelectedPatient] = useState<PatientSummary | null>(null);
  const [visitContext, setVisitContext] = useState<VisitContext>({
    visit_type: 'ambulatory',
    specialty: 'general',
    chief_complaint: null,
  });
  const [nudgeResponse, setNudgeResponse] = useState<NudgeResponse | null>(null);

  const generateMutation = useMutation({
    mutationFn: generateNudges,
    onSuccess: (response) => {
      setNudgeResponse(response);
      if (response.trace) {
        onTraceAvailable(response.trace);
      }
    },
  });

  const handleGenerate = () => {
    if (!selectedPatient) return;

    generateMutation.mutate({
      patient_id: selectedPatient.id,
      visit_context: visitContext,
      config: {
        max_nudges: 5,
        capture_trace: true,
      },
    });
  };

  return (
    <div className="max-w-[1800px] mx-auto px-6 py-8">
      <div className="grid grid-cols-12 gap-6">
        {/* Left Column - Patient Selection */}
        <div className="col-span-12 lg:col-span-4 space-y-6">
          <PatientSelector
            selectedPatient={selectedPatient}
            onSelectPatient={setSelectedPatient}
          />

          {selectedPatient && (
            <VisitContextForm
              value={visitContext}
              onChange={setVisitContext}
              onGenerate={handleGenerate}
              isGenerating={generateMutation.isPending}
            />
          )}
        </div>

        {/* Right Column - Patient Data & Results */}
        <div className="col-span-12 lg:col-span-8 space-y-6">
          {selectedPatient ? (
            <>
              <PatientViewer patient={selectedPatient} />

              {generateMutation.isPending && (
                <Card>
                  <div className="py-12 text-center">
                    <div className="relative w-16 h-16 mx-auto mb-4">
                      <div className="absolute inset-0 border-4 border-[--color-accent-primary]/20 rounded-full" />
                      <div className="absolute inset-0 border-4 border-[--color-accent-primary] border-t-transparent rounded-full animate-spin" />
                      <div className="absolute inset-2 bg-[--color-accent-primary]/10 rounded-full heartbeat" />
                    </div>
                    <p className="text-[--color-text-primary] font-medium">Generating nudges...</p>
                    <p className="text-sm text-[--color-text-muted] mt-1">
                      Analyzing patient data and searching clinical guidelines
                    </p>
                  </div>
                </Card>
              )}

              {generateMutation.isError && (
                <Card>
                  <div className="py-8 text-center">
                    <p className="text-[--color-urgent] font-medium">Generation failed</p>
                    <p className="text-sm text-[--color-text-muted] mt-1">
                      {generateMutation.error?.message || 'An unexpected error occurred'}
                    </p>
                  </div>
                </Card>
              )}

              {nudgeResponse && !generateMutation.isPending && (
                <NudgeResults response={nudgeResponse} />
              )}
            </>
          ) : (
            <Card>
              <div className="py-16 text-center">
                <div className="w-20 h-20 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-[--color-accent-primary]/20 to-[--color-accent-secondary]/20 flex items-center justify-center">
                  <svg
                    className="w-10 h-10 text-[--color-accent-primary]"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={1.5}
                      d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                    />
                  </svg>
                </div>
                <h3 className="font-display text-xl text-[--color-text-primary]">
                  Select a Patient
                </h3>
                <p className="text-[--color-text-muted] mt-2 max-w-md mx-auto">
                  Choose a patient from the list to view their clinical data and generate
                  evidence-based nudges.
                </p>
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
