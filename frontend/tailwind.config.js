/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"DM Sans"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
        display: ['"Syne"', 'sans-serif'],
      },
      colors: {
        // Base — near-black with a very slight warm tint
        base: {
          950: '#0c0c0f',
          900: '#131318',
          800: '#1e1e26',
          700: '#2c2c38',
          600: '#40404e',
          500: '#585868',
          400: '#808094',
          300: '#a0a0b4',
          200: '#c0c0d0',
          100: '#dcdce8',
          50:  '#f0f0f6',
        },
        // Accent — electric green (profit, active, success)
        accent: {
          DEFAULT: '#00e5a0',
          dim:     '#00b87f',
          muted:   '#1a3d33',
        },
        // Loss / danger
        loss: {
          DEFAULT: '#ff4d6a',
          dim:     '#cc2a45',
          muted:   '#3d1520',
        },
        // Neutral amber — warnings, pending
        warn: {
          DEFAULT: '#f5a623',
          muted:   '#3d2a0a',
        },
      },
      boxShadow: {
        'panel': '0 0 0 1px rgba(255,255,255,0.06)',
        'glow-accent': '0 0 20px rgba(0,229,160,0.15)',
        'glow-loss': '0 0 20px rgba(255,77,106,0.15)',
      },
      animation: {
        'fade-in': 'fadeIn 0.2s ease-out',
        'slide-up': 'slideUp 0.25s ease-out',
        'pulse-slow': 'pulse 3s ease-in-out infinite',
      },
      keyframes: {
        fadeIn:  { from: { opacity: 0 }, to: { opacity: 1 } },
        slideUp: { from: { opacity: 0, transform: 'translateY(8px)' }, to: { opacity: 1, transform: 'translateY(0)' } },
      },
    },
  },
  plugins: [],
}
