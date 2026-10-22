type ValidationIssue = { location: (string | number)[]; type: string };

const fieldLabels: Record<string, string> = {
  name: "Имя / название",
  phone: "Телефон",
  consent_source: "Источник согласия",
  crm_contact_id: "ID контакта amoCRM",
  tenant_id: "ID пространства",
  login: "Логин",
  password: "Пароль",
  contact_ids: "Контакты кампании",
  max_channels: "Каналы",
  window_start: "С какого часа",
  window_end: "До какого часа",
};

function validationMessage(fields: ValidationIssue[]): string {
  const messages = fields.map(({ location, type }) => {
    const field = location[1];
    // Only fixed descriptions are rendered; never echo submitted values or server text.
    if (type === "phone_e164" || (field === "phone" && type === "value_error"))
      return "Телефон: укажите международный номер с +, например +79991234567.";
    if (type === "consent_source_required")
      return "Источник согласия: укажите основание согласия на звонок.";
    const label = typeof field === "string" ? fieldLabels[field] : undefined;
    if (!label) return "Проверьте заполненные поля и источник согласия.";
    if (type === "missing" || type === "string_too_short")
      return `${label}: заполните поле.`;
    if (type === "string_too_long") return `${label}: сократите значение.`;
    if (field === "crm_contact_id")
      return "ID контакта amoCRM: укажите целое число больше нуля или оставьте поле пустым.";
    if (field === "tenant_id") return "ID пространства: укажите UUID, выданный администратором.";
    return `${label}: проверьте значение.`;
  });
  return [...new Set(messages)].join(" ") || "Проверьте заполненные поля.";
}

function validationFields(value: unknown): ValidationIssue[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is ValidationIssue =>
    item !== null && typeof item === "object" &&
    Array.isArray(item.location) &&
    item.location.every((part: unknown) => typeof part === "string" || typeof part === "number") &&
    typeof item.type === "string",
  );
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public requestId?: string,
    public fields: ValidationIssue[] = [],
  ) {
    super(code === "validation_failed" ? validationMessage(fields) : code);
  }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const csrf =
    document.cookie
      .split("; ")
      .find((c) => c.startsWith("ats_csrf="))
      ?.slice(9) ?? "";
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrf,
      ...options.headers,
    },
  });
  if (response.status === 204) return undefined as T;
  const result = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new ApiError(
      response.status,
      result.error ?? "request_failed",
      result.request_id,
      validationFields(result.fields),
    );
  return result as T;
}
