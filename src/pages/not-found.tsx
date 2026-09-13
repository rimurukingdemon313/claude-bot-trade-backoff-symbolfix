import { AlertCircle } from 'lucide-react';
import { Link } from 'wouter';

export default function NotFound() {
  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-slate-950 p-6 text-slate-100">
      <div className="w-full max-w-sm rounded-2xl border border-slate-800 bg-slate-900/60 p-6 text-center">
        <AlertCircle className="mx-auto h-8 w-8 text-amber-400" aria-hidden />
        <h1 className="mt-3 text-lg font-semibold">Page not found</h1>
        <p className="mt-2 text-xs text-slate-400">
          This dashboard has a single page. Nothing here affects the trading process.
        </p>
        <Link
          href="/"
          className="mt-4 inline-block rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-xs font-medium text-slate-200 hover:bg-slate-800"
        >
          Back to the dashboard
        </Link>
      </div>
    </div>
  );
}
