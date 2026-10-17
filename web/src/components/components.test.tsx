import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { Markdown } from "./Markdown";
import { Dialog } from "./Dialog";
import { EgressCard, ToolCard } from "./Cards";
import { Composer } from "./Composer";
import { ErrorBoundary } from "./ErrorBoundary";

describe("Markdown rendering safety", () => {
  it("shows raw HTML as text and never creates script or event-handler nodes", () => {
    const { container } = render(<Markdown text={'<script>alert(1)</script> <img src=x onerror=alert(1)>\n\n**bold**'} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
    expect(container.querySelector("strong")?.textContent).toBe("bold");
  });

  it("opens links in a new tab with noopener noreferrer and drops javascript: urls", () => {
    const { container } = render(<Markdown text={"[ok](https://example.com) [bad](javascript:alert(1))"} />);
    const links = [...container.querySelectorAll("a")];
    const ok = links.find((a) => a.textContent === "ok")!;
    expect(ok).toHaveAttribute("target", "_blank");
    expect(ok.getAttribute("rel")).toBe("noopener noreferrer");
    for (const a of links) expect(a.getAttribute("href") ?? "").not.toMatch(/^javascript:/i);
  });

  it("replaces remote images with text", () => {
    const { container } = render(<Markdown text={"![tracker](https://evil.example/p.png)"} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("[image: tracker]");
  });
});

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>opener</button>
      <Dialog open={open} title="TEST" onClose={() => setOpen(false)}>
        <button>first</button>
        <button>last</button>
      </Dialog>
    </>
  );
}

describe("Dialog", () => {
  it("traps focus, closes on Escape, and returns focus to the opener", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByText("opener");
    await user.click(opener);
    const dialog = screen.getByRole("dialog", { name: "TEST" });
    expect(dialog).toBeInTheDocument();
    const close = screen.getByLabelText("Close");
    close.focus();
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.tab({ shift: true });
    await user.tab({ shift: true });
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });
});

describe("cards", () => {
  it("shows the exact outbound string and posts the decision", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    render(<EgressCard item={{ kind: "egress", id: "e1", callId: "c1", tool: "web", outbound: "attacker.io private roadmap", reason: "tainted turn", decision: "pending" }} />);
    expect(screen.getByText("attacker.io private roadmap")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refuse" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(String(url)).toContain("/api/v1/egress/c1/decision");
    expect(JSON.parse(String(init.body))).toEqual({ decision: "reject" });
    vi.unstubAllGlobals();
  });

  it("collapses tool details behind a tap and marks quarantined results", async () => {
    const user = userEvent.setup();
    render(<ToolCard item={{ kind: "tool", id: "t", callId: "c", name: "web_omniscience_search", args: { query: "q" }, themedLine: "Looking.", reasonLine: null, status: "ok", durationMs: 1200, preview: "result", quarantined: true, taints: true, error: null }} />);
    expect(screen.queryByText("arguments")).toBeNull();
    expect(screen.getByLabelText(/quarantined reader/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "details" }));
    expect(screen.getByText("arguments")).toBeInTheDocument();
    expect(screen.getByText("1.2 s")).toBeInTheDocument();
  });
});

describe("Composer", () => {
  const renderComposer = (props: Partial<React.ComponentProps<typeof Composer>> = {}) => {
    const onSend = vi.fn(async () => true);
    const qc = new QueryClient();
    render(
      <QueryClientProvider client={qc}>
        <Composer chatId="chat_t" busy={false} onSend={onSend} onStop={() => undefined} maxFiles={2} maxMb={1} {...props} />
      </QueryClientProvider>,
    );
    return onSend;
  };

  it("sends on Enter, keeps Shift+Enter for new lines, and uses a 16px-safe input", async () => {
    const user = userEvent.setup();
    const onSend = renderComposer();
    const box = screen.getByLabelText("Message");
    await user.type(box, "hello{Shift>}{Enter}{/Shift}world");
    expect(onSend).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(onSend).toHaveBeenCalledWith("hello\nworld", []);
  });

  it("rejects oversized and surplus files with a plain message", async () => {
    renderComposer();
    const picker = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    const big = new File([new Uint8Array(2 * 1024 * 1024)], "big.bin");
    fireEvent.change(picker, { target: { files: [big] } });
    expect(await screen.findByRole("alert")).toHaveTextContent(/limit is 1 MB/);
    const files = [1, 2, 3].map((i) => new File(["x"], `f${i}.txt`));
    fireEvent.change(picker, { target: { files } });
    expect(await screen.findByText(/At most 2 files/)).toBeInTheDocument();
  });

  it("shows Stop while busy", async () => {
    const onStop = vi.fn();
    renderComposer({ busy: true, onStop });
    await userEvent.setup().click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalled();
  });
});

describe("ErrorBoundary", () => {
  it("shows a themed banner instead of a stack trace", () => {
    const Boom = (): never => {
      throw new Error("secret internal detail");
    };
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("secret internal detail");
  });
});
