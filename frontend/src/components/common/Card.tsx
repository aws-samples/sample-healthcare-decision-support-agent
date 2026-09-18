import type { ReactNode } from 'react';

interface CardProps {
  children: ReactNode;
  className?: string;
  title?: string;
  subtitle?: string;
  action?: ReactNode;
}

export function Card({ children, className = '', title, subtitle, action }: CardProps) {
  return (
    <div className={`glass-card p-5 ${className}`}>
      {(title || action) && (
        <div className="flex items-start justify-between mb-4">
          <div>
            {title && (
              <h3 className="font-display text-lg text-[--color-text-primary]">
                {title}
              </h3>
            )}
            {subtitle && (
              <p className="text-sm text-[--color-text-muted] mt-0.5">{subtitle}</p>
            )}
          </div>
          {action}
        </div>
      )}
      {children}
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: string | number;
  icon?: ReactNode;
  trend?: 'up' | 'down' | 'neutral';
  color?: string;
}

export function StatCard({ label, value, icon, color = '--color-accent-primary' }: StatCardProps) {
  return (
    <div className="glass-card p-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-[--color-text-muted]">{label}</p>
          <p className="text-2xl font-semibold mt-1" style={{ color: `var(${color})` }}>
            {value}
          </p>
        </div>
        {icon && (
          <div
            className="w-10 h-10 rounded-lg flex items-center justify-center"
            style={{ backgroundColor: `color-mix(in srgb, var(${color}) 20%, transparent)` }}
          >
            {icon}
          </div>
        )}
      </div>
    </div>
  );
}
