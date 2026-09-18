// Patient types
export interface PatientSummary {
  id: string;
  name: string;
  dob: string | null;
  gender: string | null;
  source: 'synthea' | 'medicare';
  format: 'ccda' | 'fhir';
  file_path: string;
}

export interface PatientListResponse {
  patients: PatientSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface Demographics {
  name: { given?: string; family?: string } | null;
  dob: string | null;
  gender: string | null;
}

export interface Problem {
  description: string;
  code: string | null;
  code_system: string | null;
  status: string | null;
  onset_date: string | null;
}

export interface Medication {
  name: string;
  dosage: string | null;
  route: string | null;
  status: string | null;
  start_date: string | null;
  end_date: string | null;
}

export interface Allergy {
  substance: string;
  reaction: string | null;
  severity: string | null;
}

export interface Vital {
  name: string;
  value: string;
  unit: string | null;
  date: string | null;
}

export interface Lab {
  test: string;
  value: string;
  unit: string | null;
  status: string | null;
  date: string | null;
}

export interface Immunization {
  vaccine: string;
  date: string | null;
}

export interface Encounter {
  type: string | null;
  date: string | null;
  provider: string | null;
  location: string | null;
}

export interface Procedure {
  description: string;
  code: string | null;
  date: string | null;
  status: string | null;
}

export interface ParsedPatient {
  id: string;
  format: 'ccda' | 'fhir';
  demographics: Demographics | null;
  problems: Problem[];
  medications: Medication[];
  allergies: Allergy[];
  vitals: Vital[];
  labs: Lab[];
  immunizations: Immunization[];
  encounters: Encounter[];
  procedures: Procedure[];
  clinical_notes: string[];
}

// Nudge types
export interface VisitContext {
  visit_type: 'ambulatory' | 'inpatient';
  specialty: string;
  chief_complaint: string | null;
}

export interface NudgeConfig {
  max_nudges: number;
  capture_trace: boolean;
}

export interface GuidelineCitation {
  source: string;
  section: string | null;
}

export interface Nudge {
  title: string;
  description: string;
  urgency: 'informational' | 'warning' | 'urgent';
  category: string;
  nudge_type: string;
  action_type: string;
  rationale: string;
  grounding: string;
  guideline_citation: GuidelineCitation | null;
  icd_codes: string[];
  cpt_codes: string[];
}

export interface NudgeMetadata {
  model_version: string;
  processing_time_ms: number;
  guidelines_used: string[];
}

export interface ToolCallTrace {
  tool_name: string;
  tool_use_id: string;
  input_params: Record<string, unknown>;
  output: string | null;
  duration_ms: number;
  success: boolean;
  error: string | null;
  // AgentCore OTEL-specific fields (optional)
  span_id?: string;  // For looking up tool_inputs/tool_outputs
  description?: string;
  json_schema?: string;
}

export interface Timing {
  total_ms: number;
  tool_ms: number;
  model_ms: number;
}

// Token usage types for AgentCore traces
export interface TokenUsagePerSpan {
  span_id: string;
  span_name: string;
  input_tokens: number | null;
  output_tokens: number | null;
  model: string | null;
  time_to_first_token?: number | null;
  request_duration?: number | null;
}

export interface TokenUsage {
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
  per_span: TokenUsagePerSpan[];
}

export interface ExecutionTrace {
  system_prompt: string;
  user_prompt: string;
  tool_calls: ToolCallTrace[];
  messages: TraceMessage[];
  raw_response: string | null;
  timing: Timing;
  source?: 'local' | 'agentcore';  // Trace source mode
  // AgentCore-specific rich data (populated when runtime logs are available)
  tool_inputs?: Record<string, unknown>;
  tool_outputs?: Record<string, unknown>;
  token_usage?: TokenUsage;
}

export interface NudgeResponse {
  status: 'success' | 'partial' | 'error';
  warnings: string[];
  error: string | null;
  patient_summary: string | null;
  nudges: Nudge[];
  metadata: NudgeMetadata;
  trace: ExecutionTrace | null;
}

// Admin types
export interface PromptInfo {
  name: string;
  path: string;
  category: string;
}

export interface ConfigResponse {
  specialties: string[];
  visit_types: string[];
  model_version: string;
  guidelines_path: string;
}

// Results Viewer types
export interface InferenceResult {
  sample_id: string;
  file_path: string;
  format: string;
  status: 'success' | 'partial' | 'error';
  latency_ms: number;
  nudge_count: number;
  categories_used: string[];
  nudge_types_used: string[];
  error: string | null;
  warnings: string[];
  nudges: Nudge[];
  patient_summary?: string | null;
}

export interface InferenceRun {
  timestamp: string;
  mode: 'local' | 'agentcore';
  total_samples: number;
  success_count: number;
  partial_count: number;
  error_count: number;
  avg_latency_ms: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  category_distribution: Record<string, number>;
  nudge_type_distribution: Record<string, number>;
  error_types: Record<string, number>;
  results: InferenceResult[];
}

// Message content types for trace files (both local and agentcore modes)
export interface MessageContent {
  text?: string;
  toolUse?: {
    toolUseId: string;
    name: string;
    input: Record<string, unknown>;
  };
  toolResult?: {
    toolUseId: string;
    status: string;
    content: Array<{ text: string }>;
  };
}

export interface TraceMessage {
  role: 'assistant' | 'user';
  content: MessageContent[];
}

// AgentCore OTEL span type
export interface AgentCoreSpan {
  span_id: string;
  span_name: string;
  duration_ms: number;
  status_code: string;
  attributes: {
    // Tool execution attributes
    'gen_ai.tool.call.id'?: string;
    'gen_ai.tool.name'?: string;
    'gen_ai.tool.status'?: string;
    'gen_ai.tool.description'?: string;
    'gen_ai.tool.json_schema'?: string;
    // Model/chat attributes
    'gen_ai.usage.input_tokens'?: number;
    'gen_ai.usage.output_tokens'?: number;
    'gen_ai.usage.total_tokens'?: number;
    'gen_ai.request.model'?: string;
    'gen_ai.response.finish_reasons'?: string[];
    'gen_ai.server.request.duration'?: number;
    'gen_ai.server.time_to_first_token'?: number;
    [key: string]: unknown;
  };
}

export interface TraceFile {
  sample_id: string;
  source?: 'agentcore_otel';  // Present only for agentcore mode
  system_prompt?: string;
  user_prompt?: string;
  tool_calls?: ToolCallTrace[];  // Local mode
  messages?: TraceMessage[];
  spans?: AgentCoreSpan[];  // AgentCore mode
  // AgentCore-specific rich data from runtime logs
  tool_inputs?: Record<string, unknown>;
  tool_outputs?: Record<string, unknown>;
  token_usage?: TokenUsage;
  timing?: Timing;
}

export interface TraceIndex {
  timestamp: string;
  mode: string;
  total_samples: number;
  traces: Array<{
    sample_id: string;
    file: string;
    timing: { total_ms: number; tool_ms: number };
  }>;
}
