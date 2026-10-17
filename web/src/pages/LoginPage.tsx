import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { errorMessage } from "@/lib/format";

export function LoginPage() {
  const qc = useQueryClient();
  const [pass, setPass] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await api.login(pass);
      setPass("");
      await qc.invalidateQueries({ queryKey: ["session"] });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-4">
      <h1 className="font-display text-xl tracking-widest text-neon-soft glow">ULTRON PRIME</h1>
      <p className="mt-1 text-muted">The door is locked. Say the word.</p>
      <form
        className="mt-6 space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="label" htmlFor="passphrase">
          Operator passphrase
        </label>
        <input
          id="passphrase"
          className="field"
          type="password"
          autoComplete="current-password"
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          autoFocus
        />
        {error && (
          <p role="alert" className="text-neon-soft">
            {error}
          </p>
        )}
        <button className="btn btn-primary w-full" disabled={busy || !pass}>
          Enter
        </button>
      </form>
    </main>
  );
}
