import { useState, useEffect } from 'react';
import { ChevronDown, ChevronRight, Users, Clock, Activity, User, FileText, Pill, Stethoscope, FlaskConical } from 'lucide-react';
import type { InferenceRun, InferenceResult, TraceFile, TraceIndex, ExecutionTrace, ToolCallTrace } from '../types';
import { Card, StatCard } from '../components/common/Card';
import { Badge } from '../components/common/Badge';
import { NudgeCard } from '../components/nudge/NudgeCard';
import { ExecutionTracePanel } from '../components/admin/ExecutionTracePanel';

interface ResultsManifest {
  runs: string[];
  generated_at: string;
}

// Parse patient data from user_prompt in trace file
interface ParsedPatientData {
  demographics: {
    name: { given?: string; family?: string };
    dob: string;
    gender: string;
  };
  problems: Array<{ description: string; status: string }>;
  medications: Array<{ name: string; status: string }>;
  vitals: Array<{ name: string; value: string; unit: string | null; date: string | null }>;
  labs: Array<{ test: string; value: string; unit: string | null; date: string | null }>;
}

function parsePatientDataFromPrompt(userPrompt: string): ParsedPatientData | null {
  try {
    // Standard format (local mode)
    // Format: "Structured Patient Data:\n{...}\n\nRaw Document"
    const jsonMatch = userPrompt.match(/Structured Patient Data:\s*(\{[\s\S]*?\})\s*\n\nRaw Document/);
    if (jsonMatch) {
      return JSON.parse(jsonMatch[1]);
    }

    // AgentCore format: escaped JSON in Python dict string
    // Format: "Structured Patient Data:\\n{...}\\n\\nRaw Document"
    const escapedMatch = userPrompt.match(/Structured Patient Data:\\n(\{[\s\S]*?\})\\n\\nRaw Document/);
    if (escapedMatch) {
      // Unescape the JSON string
      const unescaped = escapedMatch[1]
        .replace(/\\"/g, '"')
        .replace(/\\n/g, '\n')
        .replace(/\\\\/g, '\\');
      return JSON.parse(unescaped);
    }

    return null;
  } catch {
    return null;
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return 'Unknown';
  // Format: YYYYMMDDHHMMSS or ISO
  if (dateStr.length === 14 || dateStr.length >= 8) {
    const year = dateStr.slice(0, 4);
    const month = dateStr.slice(4, 6);
    const day = dateStr.slice(6, 8);
    return `${month}/${day}/${year}`;
  }
  return dateStr;
}

function extractPatientName(sampleId: string): string {
  // Sample ID format: FirstName_LastName_UUID
  const parts = sampleId.split('_');
  if (parts.length >= 2) {
    return `${parts[0]} ${parts[1]}`.replace(/\d+/g, '');
  }
  return sampleId;
}

// Build unified tool_calls from either local or agentcore trace format
function buildToolCalls(traceData: TraceFile): ToolCallTrace[] {
  if (traceData.source === 'agentcore_otel' && traceData.spans) {
    // AgentCore mode: extract from spans where span_name starts with "execute_tool "
    return traceData.spans
      .filter(span => span.span_name.startsWith('execute_tool '))
      .map(span => ({
        tool_name: span.attributes['gen_ai.tool.name'] as string || span.span_name.replace('execute_tool ', ''),
        tool_use_id: span.attributes['gen_ai.tool.call.id'] as string || '',
        input_params: {},  // Will be looked up from tool_inputs using span_id
        output: null,      // Will be looked up from tool_outputs using span_id
        duration_ms: span.duration_ms,
        success: span.status_code === 'OK',
        error: span.status_code !== 'OK' ? span.status_code : null,
        // AgentCore-specific metadata from OTEL spans
        span_id: span.span_id,  // For looking up tool_inputs/tool_outputs
        description: span.attributes['gen_ai.tool.description'] as string | undefined,
        json_schema: span.attributes['gen_ai.tool.json_schema'] as string | undefined,
      }));
  }
  // Local mode: use tool_calls directly
  return traceData.tool_calls || [];
}

interface PatientHistoryProps {
  patientData?: ParsedPatientData | null;
  patientSummary?: string | null;
}

function PatientHistory({ patientData, patientSummary }: PatientHistoryProps) {
  const [expanded, setExpanded] = useState(false);

  const activeProblems = patientData?.problems.filter(p => p.status === 'active') || [];
  const recentVitals = patientData?.vitals.slice(-6) || [];
  const recentLabs = patientData?.labs.slice(-5) || [];

  // Get demographics display text
  const demographicsText = patientData?.demographics
    ? `${patientData.demographics.name.given} ${patientData.demographics.name.family} | ${patientData.demographics.gender} | DOB: ${formatDate(patientData.demographics.dob)}`
    : '';

  return (
    <div className="glass-card overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full p-4 flex items-center justify-between hover:bg-[--color-bg-tertiary]/50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <User className="w-5 h-5 text-[--color-accent-primary]" />
          <div className="text-left">
            <span className="font-display text-[--color-text-primary]">Patient History</span>
            {demographicsText && (
              <span className="text-sm text-[--color-text-muted] ml-2">
                {demographicsText}
              </span>
            )}
          </div>
        </div>
        {expanded ? (
          <ChevronDown className="w-5 h-5 text-[--color-text-muted]" />
        ) : (
          <ChevronRight className="w-5 h-5 text-[--color-text-muted]" />
        )}
      </button>

      {/* Patient Summary - ALWAYS VISIBLE */}
      {patientSummary && (
        <div className="px-4 py-3 border-t border-[--color-border]">
          <p className="text-sm text-[--color-text-secondary] leading-relaxed whitespace-pre-wrap">
            {patientSummary}
          </p>
        </div>
      )}

      {/* Collapsible section - medications, vitals, labs */}
      {expanded && patientData && (
        <div className="px-4 pb-4 space-y-4 border-t border-[--color-border]">
          {/* Active Problems */}
          {activeProblems.length > 0 && (
            <div className="pt-4">
              <h4 className="flex items-center gap-2 text-sm font-semibold text-[--color-text-primary] mb-2">
                <Stethoscope className="w-4 h-4 text-[--color-text-muted]" />
                Active Problems ({activeProblems.length})
              </h4>
              <div className="flex flex-wrap gap-2">
                {activeProblems.slice(0, 8).map((problem, idx) => (
                  <span key={idx} className="px-2 py-1 text-xs bg-[--color-bg-tertiary] text-[--color-text-secondary] rounded border border-[--color-border]">
                    {problem.description}
                  </span>
                ))}
                {activeProblems.length > 8 && (
                  <span className="px-2 py-1 text-xs text-[--color-text-muted]">
                    +{activeProblems.length - 8} more
                  </span>
                )}
              </div>
            </div>
          )}

          {/* Medications */}
          {patientData.medications.length > 0 && (
            <div>
              <h4 className="flex items-center gap-2 text-sm font-semibold text-[--color-text-primary] mb-2">
                <Pill className="w-4 h-4 text-[--color-text-muted]" />
                Medications ({patientData.medications.length})
              </h4>
              <div className="flex flex-wrap gap-2">
                {patientData.medications.slice(0, 5).map((med, idx) => (
                  <span key={idx} className="px-2 py-1 text-xs bg-cyan-100 dark:bg-cyan-500/20 text-cyan-700 dark:text-cyan-400 rounded border border-cyan-200 dark:border-cyan-500/30">
                    {med.name.length > 50 ? med.name.slice(0, 50) + '...' : med.name}
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Recent Vitals */}
          {recentVitals.length > 0 && (
            <div>
              <h4 className="flex items-center gap-2 text-sm font-semibold text-[--color-text-primary] mb-2">
                <Activity className="w-4 h-4 text-[--color-text-muted]" />
                Recent Vitals
              </h4>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                {recentVitals.map((vital, idx) => (
                  <div key={idx} className="p-2 bg-[--color-bg-tertiary] rounded text-xs">
                    <span className="text-[--color-text-muted]">{vital.name.length > 30 ? vital.name.slice(0, 30) + '...' : vital.name}</span>
                    <span className="block font-semibold text-[--color-text-primary]">
                      {vital.value} {vital.unit}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Recent Labs */}
          {recentLabs.length > 0 && (
            <div>
              <h4 className="flex items-center gap-2 text-sm font-semibold text-[--color-text-primary] mb-2">
                <FlaskConical className="w-4 h-4 text-[--color-text-muted]" />
                Recent Labs
              </h4>
              <div className="grid grid-cols-2 gap-2">
                {recentLabs.map((lab, idx) => (
                  <div key={idx} className="p-2 bg-[--color-bg-tertiary] rounded text-xs">
                    <span className="text-[--color-text-muted]">{lab.test.length > 40 ? lab.test.slice(0, 40) + '...' : lab.test}</span>
                    <span className="block font-semibold text-[--color-text-primary]">
                      {parseFloat(lab.value).toFixed(2)} {lab.unit}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ResultsViewerPage() {
  const [availableRuns, setAvailableRuns] = useState<string[]>([]);
  const [selectedRunFile, setSelectedRunFile] = useState<string>('');
  const [runData, setRunData] = useState<InferenceRun | null>(null);
  const [selectedSample, setSelectedSample] = useState<InferenceResult | null>(null);
  const [traceIndex, setTraceIndex] = useState<TraceIndex | null>(null);
  const [traceData, setTraceData] = useState<TraceFile | null>(null);
  const [patientData, setPatientData] = useState<ParsedPatientData | null>(null);
  const [showTrace, setShowTrace] = useState(false);
  const [loading, setLoading] = useState(false);
  const [manifestLoading, setManifestLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Load manifest on mount
  useEffect(() => {
    async function loadManifest() {
      try {
        const response = await fetch('/results/manifest.json');
        if (!response.ok) throw new Error('Failed to load results manifest');
        const manifest: ResultsManifest = await response.json();
        setAvailableRuns(manifest.runs);
        if (manifest.runs.length > 0) {
          setSelectedRunFile(manifest.runs[0]);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load manifest');
      } finally {
        setManifestLoading(false);
      }
    }
    loadManifest();
  }, []);

  // Load run data when selection changes
  useEffect(() => {
    if (!selectedRunFile) return; // Don't load if no file selected yet

    async function loadRunData() {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(`/results/${selectedRunFile}`);
        if (!response.ok) throw new Error('Failed to load run data');
        const data: InferenceRun = await response.json();
        setRunData(data);
        setSelectedSample(null);
        setTraceData(null);
        setPatientData(null);

        // Try to load trace index
        const traceDir = selectedRunFile.replace('.json', '');
        try {
          const traceIndexResponse = await fetch(`/results/traces/${traceDir}/index.json`);
          if (traceIndexResponse.ok) {
            const indexData: TraceIndex = await traceIndexResponse.json();
            setTraceIndex(indexData);
          } else {
            setTraceIndex(null);
          }
        } catch {
          setTraceIndex(null);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    }
    loadRunData();
  }, [selectedRunFile]);

  // Load trace when sample is selected
  useEffect(() => {
    async function loadTrace() {
      if (!selectedSample || !traceIndex) return;

      const traceEntry = traceIndex.traces.find(t => t.sample_id === selectedSample.sample_id);
      if (!traceEntry) {
        setTraceData(null);
        setPatientData(null);
        return;
      }

      try {
        const traceDir = selectedRunFile.replace('.json', '');
        const response = await fetch(`/results/traces/${traceDir}/${traceEntry.file}`);
        if (response.ok) {
          const data: TraceFile = await response.json();
          setTraceData(data);

          // Parse patient data from user_prompt (if available)
          const parsed = data.user_prompt ? parsePatientDataFromPrompt(data.user_prompt) : null;
          setPatientData(parsed);
        }
      } catch {
        setTraceData(null);
        setPatientData(null);
      }
    }
    loadTrace();
  }, [selectedSample, traceIndex, selectedRunFile]);

  // Filter to only show completed samples (success or partial, not errors)
  const completedResults = runData?.results.filter(r => r.status !== 'error') || [];
  const totalNudges = completedResults.reduce((sum, r) => sum + r.nudge_count, 0);

  // Convert TraceFile to ExecutionTrace format for the panel
  const executionTrace: ExecutionTrace | null = traceData ? (() => {
    const isAgentCore = traceData.source === 'agentcore_otel';
    const totalMs = selectedSample?.latency_ms || 0;
    // Build tool calls from appropriate source (local or agentcore)
    const toolCalls = buildToolCalls(traceData);
    // Sum only positive duration values
    const toolMs = toolCalls.reduce((sum, t) => {
      const dur = t.duration_ms || 0;
      return dur > 0 ? sum + dur : sum;
    }, 0);
    const modelMs = totalMs > toolMs ? totalMs - toolMs : -1; // -1 will show as "—"

    return {
      system_prompt: traceData.system_prompt || '',
      user_prompt: traceData.user_prompt || '',
      tool_calls: toolCalls,
      messages: traceData.messages || [],
      raw_response: null,
      timing: {
        total_ms: totalMs,
        tool_ms: toolMs > 0 ? toolMs : -1,
        model_ms: modelMs,
      },
      source: isAgentCore ? 'agentcore' : 'local',
      // AgentCore-specific: pass tool_inputs/tool_outputs for input/output extraction
      tool_inputs: traceData.tool_inputs,
      tool_outputs: traceData.tool_outputs,
    };
  })() : null;

  return (
    <div className="max-w-[1600px] mx-auto px-6 py-8">
      {/* Header */}
      <div className="mb-8">
        <h2 className="font-display text-2xl text-[--color-text-primary]">Inference Results</h2>
        <p className="text-[--color-text-muted] mt-1">
          View inference results, generated nudges, and agent traces
        </p>
      </div>

      {/* Run Selector */}
      <div className="mb-6">
        <label className="block text-sm font-medium text-[--color-text-secondary] mb-2">
          Select Inference Run
        </label>
        <select
          value={selectedRunFile}
          onChange={(e) => setSelectedRunFile(e.target.value)}
          className="w-full max-w-md px-4 py-2.5 bg-[--color-bg-secondary] border border-[--color-border] rounded-lg text-[--color-text-primary] focus:outline-none focus:ring-2 focus:ring-[--color-accent-primary]/50"
        >
          {availableRuns.map((run) => {
            const parts = run.replace('.json', '').split('_');
            const mode = parts[1];
            const timestamp = `${parts[2].slice(0, 4)}-${parts[2].slice(4, 6)}-${parts[2].slice(6, 8)} ${parts[3].slice(0, 2)}:${parts[3].slice(2, 4)}`;
            return (
              <option key={run} value={run}>
                {mode.toUpperCase()} | {timestamp}
              </option>
            );
          })}
        </select>
      </div>

      {error && (
        <div className="mb-6 p-4 bg-red-100 dark:bg-red-500/20 border border-red-200 dark:border-red-500/30 rounded-lg text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {manifestLoading || loading ? (
        <div className="flex items-center justify-center py-16">
          <div className="animate-spin w-8 h-8 border-4 border-[--color-accent-primary] border-t-transparent rounded-full" />
        </div>
      ) : runData ? (
        <>
          {/* Stats Summary */}
          <div className="grid grid-cols-3 gap-4 mb-6">
            <StatCard
              label="Patient Samples"
              value={completedResults.length}
              icon={<Users className="w-5 h-5 text-[--color-accent-primary]" />}
            />
            <StatCard
              label="Avg Latency"
              value={`${(runData.avg_latency_ms / 1000).toFixed(1)}s`}
              icon={<Clock className="w-5 h-5 text-[--color-info]" />}
              color="--color-info"
            />
            <StatCard
              label="Total Nudges"
              value={totalNudges}
              icon={<Activity className="w-5 h-5 text-[--color-accent-secondary]" />}
              color="--color-accent-secondary"
            />
          </div>

          {/* Main Content */}
          <div className="grid grid-cols-12 gap-6">
            {/* Sample List */}
            <div className="col-span-12 lg:col-span-4">
              <Card title="Samples" subtitle={`${completedResults.length} patient records`}>
                <div className="max-h-[600px] overflow-y-auto space-y-2">
                  {completedResults.map((result) => (
                    <button
                      key={result.sample_id}
                      onClick={() => setSelectedSample(result)}
                      className={`w-full p-3 rounded-lg text-left transition-all ${
                        selectedSample?.sample_id === result.sample_id
                          ? 'bg-[--color-accent-primary]/10 border-2 border-[--color-accent-primary]'
                          : 'bg-[--color-bg-tertiary] border-2 border-transparent hover:border-[--color-border]'
                      }`}
                    >
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-medium text-sm text-[--color-text-primary] truncate max-w-[180px]">
                          {extractPatientName(result.sample_id)}
                        </span>
                        <Badge
                          variant={result.status === 'success' ? 'success' : result.status === 'partial' ? 'warning' : 'urgent'}
                          size="sm"
                        >
                          {result.status}
                        </Badge>
                      </div>
                      <div className="flex items-center gap-3 text-xs text-[--color-text-muted]">
                        <span className="flex items-center gap-1">
                          <FileText className="w-3 h-3" />
                          {result.format.toUpperCase()}
                        </span>
                        <span className="flex items-center gap-1">
                          <Activity className="w-3 h-3" />
                          {result.nudge_count} nudges
                        </span>
                        <span className="flex items-center gap-1">
                          <Clock className="w-3 h-3" />
                          {(result.latency_ms / 1000).toFixed(1)}s
                        </span>
                      </div>
                    </button>
                  ))}
                </div>
              </Card>
            </div>

            {/* Sample Detail */}
            <div className="col-span-12 lg:col-span-8 space-y-4">
              {selectedSample ? (
                <>
                  {/* Patient Header */}
                  <div className="flex items-center justify-between">
                    <div>
                      <h3 className="font-display text-xl text-[--color-text-primary]">
                        {extractPatientName(selectedSample.sample_id)}
                      </h3>
                      <p className="text-sm text-[--color-text-muted]">
                        {selectedSample.file_path}
                      </p>
                    </div>
                    <Badge
                      variant={selectedSample.status === 'success' ? 'success' : selectedSample.status === 'partial' ? 'warning' : 'urgent'}
                    >
                      {selectedSample.status}
                    </Badge>
                  </div>

                  {/* Patient History (with summary always visible, details collapsible) */}
                  {(patientData || selectedSample.patient_summary) && (
                    <PatientHistory
                      patientData={patientData}
                      patientSummary={selectedSample.patient_summary}
                    />
                  )}

                  {/* Nudges */}
                  {selectedSample.nudges.length > 0 ? (
                    <Card title="Clinical Nudges" subtitle={`${selectedSample.nudges.length} recommendations`}>
                      <div className="space-y-4">
                        {selectedSample.nudges.map((nudge, index) => (
                          <NudgeCard key={index} nudge={nudge} index={index} />
                        ))}
                      </div>
                    </Card>
                  ) : (
                    <Card title="Clinical Nudges">
                      <div className="py-8 text-center text-[--color-text-muted]">
                        No nudges generated for this sample
                      </div>
                    </Card>
                  )}

                  {/* Execution Trace Toggle */}
                  {executionTrace && (
                    <div className="glass-card overflow-hidden">
                      <button
                        onClick={() => setShowTrace(!showTrace)}
                        className="w-full p-4 flex items-center justify-between hover:bg-[--color-bg-tertiary]/50 transition-colors"
                      >
                        <div className="flex items-center gap-3">
                          <FileText className="w-5 h-5 text-[--color-info]" />
                          <span className="font-display text-[--color-text-primary]">Execution Trace</span>
                          <Badge variant="info" size="sm">
                            {executionTrace.tool_calls.length} tool calls
                          </Badge>
                        </div>
                        {showTrace ? (
                          <ChevronDown className="w-5 h-5 text-[--color-text-muted]" />
                        ) : (
                          <ChevronRight className="w-5 h-5 text-[--color-text-muted]" />
                        )}
                      </button>

                      {showTrace && (
                        <div className="border-t border-[--color-border]">
                          <ExecutionTracePanel trace={executionTrace} />
                        </div>
                      )}
                    </div>
                  )}
                </>
              ) : (
                <Card>
                  <div className="py-16 text-center">
                    <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-[--color-bg-tertiary] flex items-center justify-center">
                      <Users className="w-8 h-8 text-[--color-text-muted]" />
                    </div>
                    <h3 className="font-display text-lg text-[--color-text-primary]">
                      Select a Sample
                    </h3>
                    <p className="text-[--color-text-muted] mt-2 max-w-sm mx-auto">
                      Choose a patient sample from the list to view generated nudges and execution details.
                    </p>
                  </div>
                </Card>
              )}
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
