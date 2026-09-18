import { Activity, Settings, Sun, Moon, BarChart3, HeartPulse } from 'lucide-react';
import { useTheme } from '../../hooks/useTheme';

interface HeaderProps {
  activeTab: 'nudges' | 'admin' | 'results';
  onTabChange: (tab: 'nudges' | 'admin' | 'results') => void;
}

export function Header({ activeTab, onTabChange }: HeaderProps) {
  const { theme, toggleTheme } = useTheme();

  return (
    <header className="sticky top-0 z-50">
      {/* Navigation Bar */}
      <div className="border-b border-[--color-border] shadow-sm" style={{ backgroundColor: 'var(--glass-bg)' }}>
        <div className="max-w-[1800px] mx-auto px-6">
          <div className="flex items-center justify-between h-12">
            {/* Title */}
            <div className="flex items-center gap-3">
              <HeartPulse className="w-5 h-5 text-[--color-text-primary]" />
              <div className="h-4 w-px bg-[--color-border]" />
              <div className="flex items-baseline gap-2">
                <h1 className="font-semibold text-[--color-text-primary] leading-none">
                  Healthcare Decision Support
                </h1>
                <span className="text-xs text-[--color-text-muted] hidden sm:inline">
                  Clinical Nudge Generation
                </span>
              </div>
            </div>

            {/* Center Navigation */}
            <nav className="flex items-center gap-1 bg-[--color-bg-tertiary] rounded-lg p-1">
              <button
                onClick={() => onTabChange('nudges')}
                className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all ${
                  activeTab === 'nudges'
                    ? 'bg-[--color-text-primary] text-[--color-bg-secondary] shadow-sm'
                    : 'text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-bg-secondary]'
                }`}
              >
                <span className="flex items-center gap-2">
                  <Activity className="w-4 h-4" />
                  <span className="hidden sm:inline">Nudge Generation</span>
                  <span className="sm:hidden">Nudges</span>
                </span>
              </button>
              <button
                onClick={() => onTabChange('admin')}
                className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all ${
                  activeTab === 'admin'
                    ? 'bg-[--color-text-primary] text-[--color-bg-secondary] shadow-sm'
                    : 'text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-bg-secondary]'
                }`}
              >
                <span className="flex items-center gap-2">
                  <Settings className="w-4 h-4" />
                  <span className="hidden sm:inline">Admin Panel</span>
                  <span className="sm:hidden">Admin</span>
                </span>
              </button>
              <button
                onClick={() => onTabChange('results')}
                className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all ${
                  activeTab === 'results'
                    ? 'bg-[--color-text-primary] text-[--color-bg-secondary] shadow-sm'
                    : 'text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-bg-secondary]'
                }`}
              >
                <span className="flex items-center gap-2">
                  <BarChart3 className="w-4 h-4" />
                  <span>Results</span>
                </span>
              </button>
            </nav>

            {/* Theme Toggle */}
            <button
              onClick={toggleTheme}
              className="p-2 rounded-lg bg-[--color-bg-tertiary] text-[--color-text-secondary] hover:text-[--color-text-primary] hover:bg-[--color-border] transition-all"
              aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}
            >
              {theme === 'light' ? (
                <Moon className="w-4 h-4" />
              ) : (
                <Sun className="w-4 h-4" />
              )}
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
