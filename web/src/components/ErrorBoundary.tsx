import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  label?: string;
}

/** Catches render errors and shows a themed banner. Never prints a stack trace (spec 13.6). */
export class ErrorBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo): void {
    /* deliberately not rendered; the server logs carry detail */
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div role="alert" className="panel m-4 border-neon p-4">
        <p className="font-display text-neon-soft">{this.props.label ?? "A piece of the interface failed."}</p>
        <p className="mt-2 text-sm text-muted">The rest of the house is fine. Try again.</p>
        <button className="btn mt-3" onClick={() => this.setState({ failed: false })}>
          Retry
        </button>
      </div>
    );
  }
}
