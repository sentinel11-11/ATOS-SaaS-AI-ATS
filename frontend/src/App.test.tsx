import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

it("preserves a rejected contact and lets the user correct its phone", async () => {
  const contacts: unknown[] = [];
  const fetcher = vi.fn().mockImplementation((path: string, options?: RequestInit) => {
    if (path.endsWith("/auth/me"))
      return Promise.resolve(json({ login: "admin", role: "admin" }));
    if (path.endsWith("/health/ready"))
      return Promise.resolve(json({ live_calls: false }));
    if (path.endsWith("/contacts") && options?.method === "POST") {
      const data = JSON.parse(String(options.body));
      if (!data.phone.startsWith("+")) return Promise.resolve(json({
        error: "validation_failed",
        fields: [{ location: ["body", "phone"], type: "phone_e164" }],
      }, 422));
      const contact = { ...data, id: "contact-1", blocked: false };
      contacts.push(contact);
      return Promise.resolve(json(contact, 201));
    }
    return Promise.resolve(json(path.endsWith("/contacts") ? contacts : []));
  });
  vi.stubGlobal("fetch", fetcher);
  render(<App />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Контакты" }));
  await user.type(screen.getByLabelText("Имя"), "Тест");
  const phone = screen.getByLabelText(/Телефон/);
  await user.type(phone, "89990000101");
  const consent = screen.getByLabelText("Есть подтверждённое согласие на звонок");
  const source = screen.getByLabelText("Источник согласия") as HTMLInputElement;
  expect(source.required).toBe(false);
  await user.click(consent);
  expect(source.required).toBe(true);
  await user.type(source, "Собственный номер для тестирования");
  await user.click(screen.getByRole("button", { name: "Добавить" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Телефон: укажите международный номер");
  expect((phone as HTMLInputElement).value).toBe("89990000101");
  await user.clear(phone);
  await user.type(phone, "+79990000101");
  await user.click(screen.getByRole("button", { name: "Добавить" }));
  await screen.findByText("Контакт добавлен.");
  expect(screen.queryByRole("alert")).toBeNull();
  await screen.findByRole("cell", { name: "+79990000101" });
  await waitFor(() => expect(source.required).toBe(false));
  expect((phone as HTMLInputElement).value).toBe("");
  const posts = fetcher.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(JSON.parse(posts[1][1].body)).toMatchObject({
    consent: true, crm_contact_id: null, phone: "+79990000101",
  });
});
