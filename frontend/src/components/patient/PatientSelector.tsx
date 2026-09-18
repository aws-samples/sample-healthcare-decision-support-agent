import { useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Search, Database, Users, FileText } from 'lucide-react';
import { fetchPatients } from '../../api/client';
import type { PatientSummary } from '../../types';
import { Card } from '../common/Card';

interface PatientSelectorProps {
  selectedPatient: PatientSummary | null;
  onSelectPatient: (patient: PatientSummary) => void;
}

type DataSource = 'all' | 'synthea' | 'medicare';

export function PatientSelector({ selectedPatient, onSelectPatient }: PatientSelectorProps) {
  const [source, setSource] = useState<DataSource>('all');
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');

  // Debounce search
  const debounceTimeout = useCallback((value: string) => {
    const timeout = setTimeout(() => setDebouncedSearch(value), 300);
    return () => clearTimeout(timeout);
  }, []);

  const handleSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setSearch(value);
    debounceTimeout(value);
  };

  // Fetch counts for each source type (only when not searching)
  const { data: allData } = useQuery({
    queryKey: ['patients', 'all', ''],
    queryFn: () => fetchPatients({ source: 'all', limit: 200 }),
    staleTime: 60000,
  });

  // Compute counts from all data
  const ccdaCount = allData?.patients.filter(p => p.source === 'synthea').length ?? 0;
  const fhirCount = allData?.patients.filter(p => p.source === 'medicare').length ?? 0;
  const allCount = allData?.total ?? 0;

  const { data, isLoading, error } = useQuery({
    queryKey: ['patients', source, debouncedSearch],
    queryFn: () => fetchPatients({
      source,
      limit: 200,
      search: debouncedSearch || undefined,
    }),
  });

  const patients = data?.patients ?? [];
  const total = data?.total ?? 0;

  const filterButtons: { key: DataSource; label: string; count: number; colorClass: string }[] = [
    {
      key: 'all',
      label: 'All',
      count: allCount,
      colorClass: 'text-[--color-text-secondary]',
    },
    {
      key: 'synthea',
      label: 'CCDA',
      count: ccdaCount,
      colorClass: 'text-violet-600 dark:text-violet-400',
    },
    {
      key: 'medicare',
      label: 'FHIR',
      count: fhirCount,
      colorClass: 'text-emerald-600 dark:text-emerald-400',
    },
  ];

  return (
    <Card title="Patient Selection" subtitle={`${total.toLocaleString()} patients shown`}>
      {/* Source Filter */}
      <div className="flex gap-2 mb-4">
        {filterButtons.map((btn) => (
          <button
            key={btn.key}
            onClick={() => setSource(btn.key)}
            className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-all border ${
              source === btn.key
                ? btn.key === 'synthea'
                  ? 'bg-violet-600 text-white border-violet-600 dark:bg-violet-500 dark:border-violet-500'
                  : btn.key === 'medicare'
                  ? 'bg-emerald-600 text-white border-emerald-600 dark:bg-emerald-500 dark:border-emerald-500'
                  : 'bg-[--color-text-primary] text-[--color-bg-secondary] border-[--color-text-primary]'
                : 'bg-[--color-bg-secondary] text-[--color-text-secondary] border-[--color-border] hover:border-[--color-text-muted]'
            }`}
          >
            {btn.key === 'all' && <Database className={`w-4 h-4 ${source !== btn.key ? btn.colorClass : ''}`} />}
            {btn.key === 'synthea' && <Users className={`w-4 h-4 ${source !== btn.key ? btn.colorClass : ''}`} />}
            {btn.key === 'medicare' && <FileText className={`w-4 h-4 ${source !== btn.key ? btn.colorClass : ''}`} />}
            <span>{btn.label}</span>
            <span className={`text-xs px-1.5 py-0.5 rounded ${
              source === btn.key
                ? 'bg-white/20'
                : 'bg-[--color-bg-tertiary]'
            }`}>
              {btn.count}
            </span>
          </button>
        ))}
      </div>

      {/* Search */}
      <div className="relative mb-4">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[--color-text-muted]" />
        <input
          type="text"
          value={search}
          onChange={handleSearchChange}
          placeholder="Search patients by name..."
          className="w-full pl-10 pr-4 py-2.5 bg-[--color-bg-tertiary] border border-[--color-border] rounded-lg text-[--color-text-primary] placeholder-[--color-text-muted] focus:outline-none focus:border-[--color-accent-primary] transition-colors"
        />
      </div>

      {/* Patient List */}
      <div className="border border-[--color-border] rounded-lg overflow-hidden">
        {isLoading ? (
          <div className="h-[400px] flex items-center justify-center bg-[--color-bg-tertiary]/30">
            <div className="flex flex-col items-center gap-3">
              <div className="w-8 h-8 border-2 border-[--color-accent-primary] border-t-transparent rounded-full animate-spin" />
              <p className="text-sm text-[--color-text-muted]">Loading patients...</p>
            </div>
          </div>
        ) : error ? (
          <div className="h-[400px] flex items-center justify-center bg-[--color-bg-tertiary]/30">
            <p className="text-sm text-[--color-urgent]">Failed to load patients</p>
          </div>
        ) : patients.length === 0 ? (
          <div className="h-[400px] flex items-center justify-center bg-[--color-bg-tertiary]/30">
            <p className="text-sm text-[--color-text-muted]">No patients found</p>
          </div>
        ) : (
          <div className="h-[400px] overflow-y-auto bg-[--color-bg-tertiary]/30">
            <div className="p-2 space-y-1">
              {patients.map((patient) => {
                const isSelected = selectedPatient?.id === patient.id;
                return (
                  <button
                    key={patient.id}
                    onClick={() => onSelectPatient(patient)}
                    className={`w-full p-3 rounded-lg text-left transition-all ${
                      isSelected
                        ? 'bg-[--color-accent-primary]/15 border border-[--color-accent-primary]/50 shadow-sm'
                        : 'bg-[--color-bg-secondary] border border-[--color-border]/50 hover:border-[--color-border] hover:shadow-sm'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="font-medium text-[--color-text-primary]">{patient.name}</p>
                        <div className="flex items-center gap-2 mt-1">
                          <span className={`text-xs px-2 py-0.5 rounded font-medium ${
                            patient.source === 'synthea'
                              ? 'bg-violet-100 dark:bg-violet-500/20 text-violet-700 dark:text-violet-400'
                              : 'bg-emerald-100 dark:bg-emerald-500/20 text-emerald-700 dark:text-emerald-400'
                          }`}>
                            {patient.source === 'synthea' ? 'CCDA' : 'FHIR'}
                          </span>
                          <span className="text-xs text-[--color-text-muted]">
                            {patient.format.toUpperCase()}
                          </span>
                        </div>
                      </div>
                      <FileText className={`w-4 h-4 ${
                        patient.source === 'synthea'
                          ? 'text-violet-400'
                          : 'text-emerald-400'
                      }`} />
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
