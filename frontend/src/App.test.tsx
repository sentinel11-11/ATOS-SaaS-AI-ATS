import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
it("shows a session storage failure and never pretends to be logged in", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(json({ error: "storage_unavailable" }, 503)),
  );
  render(<App />);
  expect((await screen.findByRole("alert")).textContent).toContain(
    "storage_unavailable",
  );
  expect(screen.queryByText("Управление обзвоном")).toBeNull();
});
it("keeps the login screen after a rejected password", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(json({ error: "invalid_credentials" }, 401)),
      ),
  );
  render(<App />);
  const user = userEvent.setup();
  await user.type(
    await screen.findByLabelText("ID пространства"),
    "10000000-0000-0000-0000-000000000001",
  );
  await user.type(screen.getByLabelText("Логин"), "admin");
  await user.type(screen.getByLabelText("Пароль"), "fixture-only");
  await user.click(screen.getByRole("button", { name: "Войти" }));
  expect((await screen.findByRole("alert")).textContent).toContain(
    "invalid_credentials",
  );
  expect(screen.queryByText("Управление обзвоном")).toBeNull();
});
