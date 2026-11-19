import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./features/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: { "2xl": "1400px" },
    },
    extend: {
      colors: {
        // ---- shadcn semantic tokens (reference CSS vars) ----
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
          hover: "hsl(var(--accent-hover))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        chart: {
          "1": "hsl(var(--chart-1))",
          "2": "hsl(var(--chart-2))",
          "3": "hsl(var(--chart-3))",
          "4": "hsl(var(--chart-4))",
          "5": "hsl(var(--chart-5))",
        },

        // ---- Atlas design-system tokens (reference CSS vars from globals.css) ----
        "atlas-primary": {
          50: "var(--color-primary-50)",
          100: "var(--color-primary-100)",
          200: "var(--color-primary-200)",
          400: "var(--color-primary-400)",
          500: "var(--color-primary-500)",
          600: "var(--color-primary-600)",
          700: "var(--color-primary-700)",
          900: "var(--color-primary-900)",
        },
        "atlas-success": "var(--color-success)",
        "atlas-warning": "var(--color-warning)",
        "atlas-error": "var(--color-error)",
        "atlas-info": "var(--color-info)",
        "atlas-ai": {
          primary: "var(--color-ai-primary)",
          surface: "var(--color-ai-surface)",
          "gradient-start": "var(--color-ai-gradient-start)",
          "gradient-end": "var(--color-ai-gradient-end)",
        },
        "atlas-surface": {
          background: "var(--surface-background)",
          card: "var(--surface-card)",
          sidebar: "var(--surface-sidebar)",
          "sidebar-hover": "var(--surface-sidebar-hover)",
          "sidebar-active": "var(--surface-sidebar-active)",
          "table-header": "var(--surface-table-header)",
          "table-stripe": "var(--surface-table-stripe)",
        },

        // ---- shadcn bridge tokens (kept for compatibility) ----
        surface: "hsl(var(--card))",
        success: "hsl(var(--success))",
        warning: "hsl(var(--warning))",
        "in-policy": "hsl(var(--in-policy))",
        "out-policy": "hsl(var(--out-policy))",
        "text-primary": "hsl(var(--foreground))",
        "text-muted": "hsl(var(--muted-foreground))",
      },
      spacing: {
        "atlas-1": "var(--space-1)",
        "atlas-2": "var(--space-2)",
        "atlas-3": "var(--space-3)",
        "atlas-4": "var(--space-4)",
        "atlas-5": "var(--space-5)",
        "atlas-6": "var(--space-6)",
        "atlas-8": "var(--space-8)",
        "atlas-12": "var(--space-12)",
        "atlas-16": "var(--space-16)",
      },
      borderRadius: {
        // shadcn defaults
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
        // Atlas-named tokens
        "atlas-sm": "var(--radius-sm)",
        "atlas-md": "var(--radius-md)",
        "atlas-lg": "var(--radius-lg)",
        "atlas-xl": "var(--radius-xl)",
        "atlas-full": "var(--radius-full)",
      },
      boxShadow: {
        "atlas-0": "var(--shadow-0)",
        "atlas-1": "var(--shadow-1)",
        "atlas-2": "var(--shadow-2)",
        "atlas-3": "var(--shadow-3)",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      transitionTimingFunction: {
        "atlas-spring": "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
      transitionDuration: {
        "atlas-fast": "150ms",
        "atlas-normal": "200ms",
        "atlas-slow": "300ms",
      },
      keyframes: {
        // shadcn defaults
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        // Atlas animation patterns
        "skeleton-shimmer": {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        "fade-in-up": {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "slide-in-right": {
          from: { opacity: "0", transform: "translateX(100%)" },
          to: { opacity: "1", transform: "translateX(0)" },
        },
        "scale-press": {
          "0%": { transform: "scale(1)" },
          "100%": { transform: "scale(0.97)" },
        },
        "dot-bounce": {
          "0%, 80%, 100%": { transform: "scale(0)" },
          "40%": { transform: "scale(1)" },
        },
        "progress-fill": {
          from: { width: "0%" },
          to: { width: "var(--progress-width, 0%)" },
        },
        "toast-slide": {
          from: { opacity: "0", transform: "translateX(100%)" },
          to: { opacity: "1", transform: "translateX(0)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "skeleton-shimmer": "skeleton-shimmer 1.5s linear infinite",
        "fade-in-up": "fade-in-up 200ms ease forwards",
        "slide-in-right": "slide-in-right 300ms ease-out forwards",
        "scale-press": "scale-press 50ms ease forwards",
        "dot-bounce": "dot-bounce 1.4s ease-in-out infinite",
        "progress-fill": "progress-fill 500ms ease-out forwards",
        "toast-slide": "toast-slide 300ms ease-out forwards",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};
export default config;
