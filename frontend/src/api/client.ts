import axios from 'axios';
import type {
  PatientListResponse,
  ParsedPatient,
  NudgeResponse,
  VisitContext,
  NudgeConfig,
  PromptInfo,
  ConfigResponse,
} from '../types';

const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
});

// Patient endpoints
export async function fetchPatients(params: {
  source?: 'synthea' | 'medicare' | 'all';
  limit?: number;
  offset?: number;
  search?: string;
}): Promise<PatientListResponse> {
  const { data } = await api.get('/patients', { params });
  return data;
}

export async function fetchPatient(patientId: string): Promise<ParsedPatient> {
  const { data } = await api.get(`/patients/${patientId}`);
  return data;
}

// Nudge endpoints
export async function generateNudges(params: {
  patient_id: string;
  visit_context: VisitContext;
  config?: NudgeConfig;
}): Promise<NudgeResponse> {
  const { data } = await api.post('/nudges/generate', params);
  return data;
}

// Admin endpoints
export async function fetchPrompts(): Promise<{ prompts: PromptInfo[] }> {
  const { data } = await api.get('/admin/prompts');
  return data;
}

export async function fetchPromptContent(name: string): Promise<{
  name: string;
  path: string;
  content: string;
}> {
  const { data } = await api.get(`/admin/prompts/${name}`);
  return data;
}

export async function fetchSpecialties(): Promise<string[]> {
  const { data } = await api.get('/admin/specialties');
  return data;
}

export async function fetchConfig(): Promise<ConfigResponse> {
  const { data } = await api.get('/admin/config');
  return data;
}

export default api;
