import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router-dom";
import { api, setUnauthenticatedHandler } from "@/lib/api";
import { applyAppearance, useUi } from "@/store/ui";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ChatPage } from "@/pages/ChatPage";
import { LoginPage } from "@/pages/LoginPage";
import { SettingsPage } from "@/pages/SettingsPage";

export function App() {
  const qc = useQueryClient();
  const appearance = useUi((s) => s.appearance);
  useEffect(() => applyAppearance(appearance), [appearance]);
  useEffect(() => {
    setUnauthenticatedHandler(() => void qc.invalidateQueries({ queryKey: ["session"] }));
    return () => setUnauthenticatedHandler(null);
  }, [qc]);

  const session = useQuery({ queryKey: ["session"], queryFn: api.session, retry: false });
  if (session.isPending) return <p className="p-6 pulse text-muted">Waking the house…</p>;
  if (session.isError) {
    return (
      <div role="alert" className="m-4 panel border-neon p-4">
        <p className="font-display text-neon-soft">The house is unreachable.</p>
        <p className="mt-2 text-sm text-muted">The server did not answer. I will be here when it does.</p>
        <button className="btn mt-3" onClick={() => void session.refetch()}>
          Try again
        </button>
      </div>
    );
  }
  if (!session.data.authenticated) return <LoginPage />;
  return (
    <ErrorBoundary>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        <Route path="/chat/:chatId" element={<ChatPage />} />
        <Route path="/settings" element={<Navigate to="/settings/memory" replace />} />
        <Route path="/settings/:page" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </ErrorBoundary>
  );
}
