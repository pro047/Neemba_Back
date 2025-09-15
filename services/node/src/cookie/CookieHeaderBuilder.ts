import type { NetscapeCookie } from "./NetscapeCookiHeaderParser";

export class CookieHeaderBuilder {
  static build(cookies: NetscapeCookie[]): string {
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  }
}
