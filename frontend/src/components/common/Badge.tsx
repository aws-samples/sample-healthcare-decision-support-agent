import type { ReactNode } from 'react';

type BadgeVariant = 'urgent' | 'warning' | 'info' | 'success' | 'default';

interface BadgeProps {
  children: ReactNode;
  variant?: BadgeVariant;
  size?: 'sm' | 'md';
  className?: string;
}

// High-contrast badge styles that work well in both light and dark modes
const variantStyles: Record<BadgeVariant, string> = {
  urgent: 'bg-red-600 dark:bg-red-500 text-white',
  warning: 'bg-amber-500 dark:bg-amber-400 text-amber-950 dark:text-amber-950',
  info: 'bg-sky-600 dark:bg-sky-500 text-white',
  success: 'bg-emerald-600 dark:bg-emerald-500 text-white',
  default: 'bg-[--color-bg-tertiary] text-[--color-text-secondary] border border-[--color-border]',
};

const sizeStyles = {
  sm: 'px-2 py-0.5 text-xs',
  md: 'px-2.5 py-1 text-sm',
};

export function Badge({ children, variant = 'default', size = 'sm', className = '' }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center font-semibold rounded-md ${variantStyles[variant]} ${sizeStyles[size]} ${className}`}
    >
      {children}
    </span>
  );
}

interface StatusDotProps {
  status: 'active' | 'resolved' | 'pending';
  className?: string;
}

export function StatusDot({ status, className = '' }: StatusDotProps) {
  const statusColors = {
    active: 'bg-[--color-success] shadow-[0_0_8px_var(--color-success)]',
    resolved: 'bg-[--color-text-muted]',
    pending: 'bg-[--color-warning]',
  };

  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${statusColors[status]} ${className}`}
    />
  );
}
