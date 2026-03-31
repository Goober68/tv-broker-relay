import { Outlet, Link, useLocation } from 'react-router-dom'

export default function AuthLayout() {
  const { pathname } = useLocation()
  const isLogin = pathname === '/login'

  return (
    <div className="min-h-screen bg-base-950 flex items-center justify-center p-4">
      {/* Background grid pattern */}
      <div
        className="fixed inset-0 opacity-[0.03] pointer-events-none"
        style={{
          backgroundImage: `linear-gradient(#fff 1px, transparent 1px), linear-gradient(90deg, #fff 1px, transparent 1px)`,
          backgroundSize: '48px 48px',
        }}
      />

      <div className="w-full max-w-sm relative z-10 animate-slide-up">
        {/* Wordmark */}
        <div className="text-center mb-8">
          <span className="font-display font-bold text-3xl text-base-50 tracking-tight">
            relay<span className="text-accent">.</span>
          </span>
          <sup className="text-[10px] font-mono text-base-500 ml-1 -mt-3">beta</sup>
          <p className="text-base-400 text-sm mt-2">
            TradingView → Broker Execution
          </p>
        </div>

        {/* Card */}
        <div className="panel p-8 shadow-glow-accent/5">
          <Outlet />
        </div>

        {/* Switch link */}
        <p className="text-center text-sm text-base-400 mt-5">
          {isLogin ? (
            <>
              Don't have an account?{' '}
              <Link to="/register" className="text-accent hover:text-accent-dim transition-colors">
                Sign up
              </Link>
            </>
          ) : (
            <>
              Already have an account?{' '}
              <Link to="/login" className="text-accent hover:text-accent-dim transition-colors">
                Sign in
              </Link>
            </>
          )}
        </p>
      </div>
    </div>
  )
}
