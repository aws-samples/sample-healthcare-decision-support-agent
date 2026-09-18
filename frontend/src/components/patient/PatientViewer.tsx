import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { User, Heart, Pill, AlertTriangle, Activity, TestTube, Syringe, Calendar, Filter, ChevronDown, ChevronUp, FolderOpen, FolderClosed } from 'lucide-react';
import { format, parseISO, parse } from 'date-fns';
import { fetchPatient } from '../../api/client';
import type { PatientSummary, ParsedPatient } from '../../types';
import { Card } from '../common/Card';
import { Badge, StatusDot } from '../common/Badge';

/**
 * Parse a date string that may be in ISO format (FHIR) or HL7 format (CCDA).
 * HL7 format: YYYYMMDD or YYYYMMDDHHMMSS
 * ISO format: YYYY-MM-DD
 */
function parseFlexibleDate(dateStr: string): Date | null {
  if (!dateStr) return null;

  try {
    // Check if it's ISO format (contains dashes)
    if (dateStr.includes('-')) {
      return parseISO(dateStr);
    }

    // HL7 format: YYYYMMDD or YYYYMMDDHHMMSS
    if (/^\d{8,14}$/.test(dateStr)) {
      return parse(dateStr.slice(0, 8), 'yyyyMMdd', new Date());
    }

    // Fallback: try parseISO anyway
    return parseISO(dateStr);
  } catch {
    return null;
  }
}

interface PatientViewerProps {
  patient: PatientSummary;
  defaultCollapsed?: boolean;
}

type TabKey = 'problems' | 'medications' | 'allergies' | 'vitals' | 'labs' | 'immunizations';

export function PatientViewer({ patient, defaultCollapsed = false }: PatientViewerProps) {
  const [activeTab, setActiveTab] = useState<TabKey>('problems');
  const [showActiveOnly, setShowActiveOnly] = useState(false);
  const [isCollapsed, setIsCollapsed] = useState(defaultCollapsed);

  const { data, isLoading, error } = useQuery({
    queryKey: ['patient', patient.id],
    queryFn: () => fetchPatient(patient.id),
  });

  if (isLoading) {
    return (
      <Card className="h-full">
        <div className="h-[200px] flex items-center justify-center">
          <div className="flex flex-col items-center gap-3">
            <div className="w-10 h-10 border-2 border-[--color-accent-primary] border-t-transparent rounded-full animate-spin" />
            <p className="text-sm text-[--color-text-muted]">Loading patient data...</p>
          </div>
        </div>
      </Card>
    );
  }

  if (error || !data) {
    return (
      <Card className="h-full">
        <div className="h-[200px] flex items-center justify-center">
          <p className="text-sm text-[--color-urgent]">Failed to load patient data</p>
        </div>
      </Card>
    );
  }

  const tabs: { key: TabKey; label: string; icon: React.ReactNode; count: number }[] = [
    { key: 'problems', label: 'Problems', icon: <Heart className="w-4 h-4" />, count: data.problems.length },
    { key: 'medications', label: 'Medications', icon: <Pill className="w-4 h-4" />, count: data.medications.length },
    { key: 'allergies', label: 'Allergies', icon: <AlertTriangle className="w-4 h-4" />, count: data.allergies.length },
    { key: 'vitals', label: 'Vitals', icon: <Activity className="w-4 h-4" />, count: data.vitals.length },
    { key: 'labs', label: 'Labs', icon: <TestTube className="w-4 h-4" />, count: data.labs.length },
    { key: 'immunizations', label: 'Immunizations', icon: <Syringe className="w-4 h-4" />, count: data.immunizations.length },
  ];

  // Calculate summary stats for collapsed view
  const totalRecords = tabs.reduce((sum, tab) => sum + tab.count, 0);
  const activeProblems = data.problems.filter(p => p.status?.toLowerCase() === 'active').length;
  const activeMeds = data.medications.filter(m => m.status?.toLowerCase() === 'active').length;

  return (
    <div className="space-y-0">
      {/* Demographics Card with Collapsible Header */}
      <Card className={isCollapsed ? 'rounded-b-xl' : 'rounded-b-none border-b-0'}>
        <div className="flex items-start gap-4">
          <div className="w-14 h-14 rounded-xl bg-gradient-to-br from-[--color-accent-primary] to-[--color-accent-secondary] flex items-center justify-center text-white text-xl font-display flex-shrink-0">
            {(data.demographics?.name?.given || 'U').charAt(0)}
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <h2 className="font-display text-xl text-[--color-text-primary] truncate">
                  {data.demographics?.name ? `${data.demographics.name.given || ''} ${data.demographics.name.family || ''}`.trim() : 'Unknown'}
                </h2>
                <div className="flex flex-wrap items-center gap-3 mt-1.5 text-sm">
                  <span className="flex items-center gap-1.5 text-[--color-text-secondary]">
                    <Calendar className="w-3.5 h-3.5" />
                    {data.demographics?.dob
                      ? (() => {
                          const parsed = parseFlexibleDate(data.demographics.dob);
                          return parsed ? format(parsed, 'MMM d, yyyy') : data.demographics.dob;
                        })()
                      : 'Unknown'}
                  </span>
                  <span className="flex items-center gap-1.5 text-[--color-text-secondary]">
                    <User className="w-3.5 h-3.5" />
                    {data.demographics?.gender || 'Unknown'}
                  </span>
                  <Badge variant={patient.source === 'synthea' ? 'info' : 'success'} size="sm">
                    {patient.source === 'synthea' ? 'Synthea' : 'Medicare'} • {data.format.toUpperCase()}
                  </Badge>
                </div>
              </div>

              {/* Collapse Toggle Button */}
              <button
                onClick={() => setIsCollapsed(!isCollapsed)}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium transition-all bg-[--color-bg-tertiary] text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-border] flex-shrink-0"
                title={isCollapsed ? 'Show patient history' : 'Hide patient history'}
              >
                {isCollapsed ? (
                  <>
                    <FolderClosed className="w-4 h-4" />
                    <span className="hidden sm:inline">Show History</span>
                    <ChevronDown className="w-4 h-4" />
                  </>
                ) : (
                  <>
                    <FolderOpen className="w-4 h-4" />
                    <span className="hidden sm:inline">Hide History</span>
                    <ChevronUp className="w-4 h-4" />
                  </>
                )}
              </button>
            </div>

            {/* Collapsed Summary */}
            {isCollapsed && (
              <div className="flex flex-wrap items-center gap-3 mt-3 pt-3 border-t border-[--color-border]">
                <span className="text-xs text-[--color-text-muted]">Quick Summary:</span>
                <span className="px-2 py-1 bg-[--color-bg-tertiary] rounded text-xs text-[--color-text-secondary]">
                  {activeProblems} active problem{activeProblems !== 1 ? 's' : ''}
                </span>
                <span className="px-2 py-1 bg-[--color-bg-tertiary] rounded text-xs text-[--color-text-secondary]">
                  {activeMeds} medication{activeMeds !== 1 ? 's' : ''}
                </span>
                <span className="px-2 py-1 bg-[--color-bg-tertiary] rounded text-xs text-[--color-text-secondary]">
                  {data.allergies.length} allerg{data.allergies.length !== 1 ? 'ies' : 'y'}
                </span>
                <span className="px-2 py-1 bg-[--color-bg-tertiary] rounded text-xs text-[--color-text-muted]">
                  {totalRecords} total records
                </span>
              </div>
            )}
          </div>
        </div>
      </Card>

      {/* Expandable Data Tabs */}
      <div
        className={`overflow-hidden transition-all duration-300 ease-in-out ${
          isCollapsed ? 'max-h-0 opacity-0' : 'max-h-[800px] opacity-100'
        }`}
      >
        <Card className="rounded-t-none border-t border-[--color-border]/50">
          {/* Tab Navigation */}
          <div className="flex items-center justify-between mb-4 border-b border-[--color-border] pb-4">
            <div className="flex gap-1 overflow-x-auto">
              {tabs.map(tab => (
                <button
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-all ${
                    activeTab === tab.key
                      ? 'bg-[--color-text-primary] text-[--color-bg-secondary]'
                      : 'text-[--color-text-secondary] hover:bg-[--color-bg-tertiary]'
                  }`}
                >
                  {tab.icon}
                  {tab.label}
                  <span className={`text-xs px-1.5 py-0.5 rounded font-semibold ${
                    activeTab === tab.key ? 'bg-white/20' : 'bg-[--color-bg-tertiary]'
                  }`}>
                    {tab.count}
                  </span>
                </button>
              ))}
            </div>

            {/* Filter Toggle */}
            {(activeTab === 'problems' || activeTab === 'medications') && (
              <button
                onClick={() => setShowActiveOnly(!showActiveOnly)}
                className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-all border ${
                  showActiveOnly
                    ? 'bg-emerald-600 text-white border-emerald-600 dark:bg-emerald-500 dark:border-emerald-500'
                    : 'bg-[--color-bg-secondary] text-[--color-text-secondary] border-[--color-border] hover:border-[--color-text-muted]'
                }`}
              >
                <Filter className="w-4 h-4" />
                Active Only
              </button>
            )}
          </div>

          {/* Tab Content */}
          <div className="min-h-[250px] max-h-[400px] overflow-y-auto">
            {activeTab === 'problems' && <ProblemsTable data={data} showActiveOnly={showActiveOnly} />}
            {activeTab === 'medications' && <MedicationsTable data={data} showActiveOnly={showActiveOnly} />}
            {activeTab === 'allergies' && <AllergiesTable data={data} />}
            {activeTab === 'vitals' && <VitalsTable data={data} />}
            {activeTab === 'labs' && <LabsTable data={data} />}
            {activeTab === 'immunizations' && <ImmunizationsTable data={data} />}
          </div>
        </Card>
      </div>
    </div>
  );
}

function ProblemsTable({ data, showActiveOnly }: { data: ParsedPatient; showActiveOnly: boolean }) {
  const problems = showActiveOnly
    ? data.problems.filter(p => p.status?.toLowerCase() === 'active')
    : data.problems;

  if (problems.length === 0) {
    return <EmptyState message="No problems found" />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="text-left text-sm text-[--color-text-muted] border-b border-[--color-border]">
            <th className="pb-3 font-medium">Status</th>
            <th className="pb-3 font-medium">Description</th>
            <th className="pb-3 font-medium">Code</th>
            <th className="pb-3 font-medium">Onset</th>
          </tr>
        </thead>
        <tbody>
          {problems.map((problem, i) => (
            <tr key={i} className="border-b border-[--color-border]/50 hover:bg-[--color-bg-tertiary]/30">
              <td className="py-3">
                <div className="flex items-center gap-2">
                  <StatusDot status={problem.status?.toLowerCase() === 'active' ? 'active' : 'resolved'} />
                  <span className="text-sm text-[--color-text-secondary] capitalize">
                    {problem.status || 'Unknown'}
                  </span>
                </div>
              </td>
              <td className="py-3 text-[--color-text-primary]">{problem.description}</td>
              <td className="py-3 font-mono text-sm text-[--color-text-muted]">{problem.code || '-'}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{problem.onset_date || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MedicationsTable({ data, showActiveOnly }: { data: ParsedPatient; showActiveOnly: boolean }) {
  const medications = showActiveOnly
    ? data.medications.filter(m => m.status?.toLowerCase() === 'active')
    : data.medications;

  if (medications.length === 0) {
    return <EmptyState message="No medications found" />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="text-left text-sm text-[--color-text-muted] border-b border-[--color-border]">
            <th className="pb-3 font-medium">Status</th>
            <th className="pb-3 font-medium">Medication</th>
            <th className="pb-3 font-medium">Dosage</th>
            <th className="pb-3 font-medium">Route</th>
            <th className="pb-3 font-medium">Start</th>
          </tr>
        </thead>
        <tbody>
          {medications.map((med, i) => (
            <tr key={i} className="border-b border-[--color-border]/50 hover:bg-[--color-bg-tertiary]/30">
              <td className="py-3">
                <div className="flex items-center gap-2">
                  <StatusDot status={med.status?.toLowerCase() === 'active' ? 'active' : 'resolved'} />
                  <span className="text-sm text-[--color-text-secondary] capitalize">
                    {med.status || 'Unknown'}
                  </span>
                </div>
              </td>
              <td className="py-3 text-[--color-text-primary]">{med.name}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{med.dosage || '-'}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{med.route || '-'}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{med.start_date || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AllergiesTable({ data }: { data: ParsedPatient }) {
  if (data.allergies.length === 0) {
    return <EmptyState message="No allergies recorded" />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="text-left text-sm text-[--color-text-muted] border-b border-[--color-border]">
            <th className="pb-3 font-medium">Substance</th>
            <th className="pb-3 font-medium">Reaction</th>
            <th className="pb-3 font-medium">Severity</th>
          </tr>
        </thead>
        <tbody>
          {data.allergies.map((allergy, i) => (
            <tr key={i} className="border-b border-[--color-border]/50 hover:bg-[--color-bg-tertiary]/30">
              <td className="py-3 text-[--color-text-primary]">{allergy.substance}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{allergy.reaction || '-'}</td>
              <td className="py-3">
                {allergy.severity && (
                  <Badge variant={allergy.severity.toLowerCase() === 'severe' ? 'urgent' : 'warning'}>
                    {allergy.severity}
                  </Badge>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function VitalsTable({ data }: { data: ParsedPatient }) {
  if (data.vitals.length === 0) {
    return <EmptyState message="No vitals recorded" />;
  }

  // Group by date and take most recent
  const recentVitals = data.vitals.slice(0, 20);

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="text-left text-sm text-[--color-text-muted] border-b border-[--color-border]">
            <th className="pb-3 font-medium">Vital Sign</th>
            <th className="pb-3 font-medium">Value</th>
            <th className="pb-3 font-medium">Unit</th>
            <th className="pb-3 font-medium">Date</th>
          </tr>
        </thead>
        <tbody>
          {recentVitals.map((vital, i) => (
            <tr key={i} className="border-b border-[--color-border]/50 hover:bg-[--color-bg-tertiary]/30">
              <td className="py-3 text-[--color-text-primary]">{vital.name}</td>
              <td className="py-3 font-mono text-[--color-accent-primary]">{vital.value}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{vital.unit || '-'}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{vital.date || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LabsTable({ data }: { data: ParsedPatient }) {
  if (data.labs.length === 0) {
    return <EmptyState message="No lab results found" />;
  }

  const recentLabs = data.labs.slice(0, 20);

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="text-left text-sm text-[--color-text-muted] border-b border-[--color-border]">
            <th className="pb-3 font-medium">Test</th>
            <th className="pb-3 font-medium">Value</th>
            <th className="pb-3 font-medium">Unit</th>
            <th className="pb-3 font-medium">Date</th>
          </tr>
        </thead>
        <tbody>
          {recentLabs.map((lab, i) => (
            <tr key={i} className="border-b border-[--color-border]/50 hover:bg-[--color-bg-tertiary]/30">
              <td className="py-3 text-[--color-text-primary]">{lab.test}</td>
              <td className="py-3 font-mono text-[--color-accent-primary]">{lab.value}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{lab.unit || '-'}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{lab.date || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ImmunizationsTable({ data }: { data: ParsedPatient }) {
  if (data.immunizations.length === 0) {
    return <EmptyState message="No immunizations recorded" />;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead>
          <tr className="text-left text-sm text-[--color-text-muted] border-b border-[--color-border]">
            <th className="pb-3 font-medium">Vaccine</th>
            <th className="pb-3 font-medium">Date</th>
          </tr>
        </thead>
        <tbody>
          {data.immunizations.map((imm, i) => (
            <tr key={i} className="border-b border-[--color-border]/50 hover:bg-[--color-bg-tertiary]/30">
              <td className="py-3 text-[--color-text-primary]">{imm.vaccine}</td>
              <td className="py-3 text-sm text-[--color-text-secondary]">{imm.date || '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <div className="h-[200px] flex items-center justify-center">
      <p className="text-sm text-[--color-text-muted]">{message}</p>
    </div>
  );
}
