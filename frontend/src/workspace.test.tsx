import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Editor } from "./workspace";
import { BlacklistView } from "./WorkspaceResources";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const json = (value: unknown) =>
  new Response(JSON.stringify(value), { status: 200 });

it("restores the workspace navigation for administrators", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation((path: string) =>
        Promise.resolve(
          json(
            path.endsWith("/auth/me")
              ? { login: "admin", role: "admin" }
              : { counts: {}, tenant: {}, active_calls: [] },
          ),
        ),
      ),
  );
  render(<App />);
  for (const title of [
    "Дашборд",
    "Кампании",
    "Контакты и базы",
    "Журнал звонков",
    "Пул номеров",
    "Операторы и очередь",
    "Сценарии",
    "Чёрный список",
    "Отчёты",
    "Пользователи",
    "Настройки",
    "Логи",
  ]) {
    await screen.findByRole("button", { name: title });
  }
});

it("does not expose administrator screens to an operator", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation((path: string) =>
        Promise.resolve(
          json(
            path.endsWith("/auth/me")
              ? { login: "operator", role: "operator" }
              : { counts: {}, tenant: {}, active_calls: [] },
          ),
        ),
      ),
  );
  render(<App />);
  await screen.findByRole("button", { name: "Дашборд" });
  expect(screen.queryByRole("button", { name: "Пользователи" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Настройки" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Логи" })).toBeNull();
});

it("sends selected weekdays as numbers", async () => {
  const submit = vi.fn().mockResolvedValue(undefined);
  render(
    <Editor
      fields={[{ name: "weekdays", label: "Дни", type: "weekdays" }]}
      initial={{ weekdays: [0, 1] }}
      busy={false}
      submit={submit}
    />,
  );
  const user = userEvent.setup();
  await user.click(screen.getByLabelText("Вт"));
  await user.click(screen.getByLabelText("Ср"));
  await user.click(screen.getByRole("button", { name: "Сохранить" }));
  await waitFor(() =>
    expect(submit).toHaveBeenCalledWith({ weekdays: [0, 2] }),
  );
});

it("paginates the blacklist instead of hiding records after the first fifty", async () => {
  const fetcher = vi.fn().mockImplementation((path: string) => {
    const offset = Number(
      new URL(path, "http://localhost").searchParams.get("offset"),
    );
    return Promise.resolve(
      json({
        total: 53,
        items: [
          {
            id: String(offset),
            phone: `contact-${offset}`,
            reason: "Test",
            created_at: "2026-10-01T00:00:00Z",
          },
        ],
      }),
    );
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <BlacklistView
      admin={false}
      busy={false}
      action={async (fn) => fn()}
      setNotice={() => {}}
    />,
  );
  await screen.findByText("contact-0");
  await userEvent.setup().click(screen.getByRole("button", { name: "Далее" }));
  await screen.findByText("contact-50");
  expect(screen.queryByText("contact-0")).toBeNull();
});

it("shows operator status controls only to an operator", async () => {
  const { OperatorsView } = await import("./WorkspaceAdmin");
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(json({ operators: [], queue: [] })),
      ),
  );
  const props = {
    admin: true,
    role: "admin",
    busy: false,
    action: async (fn: () => Promise<void>) => fn(),
    setNotice: () => {},
  };
  const view = render(<OperatorsView {...props} />);
  expect(screen.queryByRole("button", { name: "Готов к работе" })).toBeNull();
  view.rerender(<OperatorsView {...props} admin={false} role="operator" />);
  await screen.findByRole("button", { name: "Готов к работе" });
});
