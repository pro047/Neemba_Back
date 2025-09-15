import type { NetscapeCookie } from "./NetscapeCookiHeaderParser";

export type CookieFilterOptions = {
  requestUrl: string;
  allowedNames?: string[];
  nowEpochSeconds?: number;
};

export class CookieFilter {
  constructor(private options: CookieFilterOptions) {}

  filter(cookies: NetscapeCookie[]): NetscapeCookie[] {
    const url = new URL(this.options.requestUrl);
    const host = url.hostname;
    const path = url.pathname || "/";
    const now = this.options.nowEpochSeconds ?? Math.floor(Date.now() / 1000);

    const allowedNamesSet = new Set(
      this.options.allowedNames && this.options.allowedNames.length > 0
        ? this.options.allowedNames
        : [
            "__Secure-3PSID",
            "__Secure-3PAPISID",
            "SAPISID",
            "APISID",
            "HSID",
            "SSID",
            "SID",
            "VISITOR_INFO1_LIVE",
            "PREF",
            "LOGIN_INFO",
            "SIDCC",
            "__Secure-1PSID",
            "__Secure-3PSIDCC",
            "__Secure-1PSIDTS",
            "__Secure-3PSIDTS",
          ]
    );

    const domainMatches = (cookieDomain: string): boolean => {
      const cd = cookieDomain.startsWith(".")
        ? cookieDomain.slice(1)
        : cookieDomain;
      return host === cd || host.endsWith(`.${cd}`);
    };

    const pathMatches = (cookiePath: string): boolean => {
      return path.startsWith(cookiePath);
    };

    const scoped = cookies.filter(
      (c) =>
        c.value &&
        (!c.expiresAt || c.expiresAt > now) &&
        domainMatches(c.domain) &&
        pathMatches(c.path) &&
        allowedNamesSet.has(c.name)
    );

    const dedup = new Map<string, NetscapeCookie>();
    for (const c of scoped) {
      const existing = dedup.get(c.name);
      if (!existing) {
        dedup.set(c.name, c);
        continue;
      }

      const score = (d: string) =>
        d.startsWith(".") ? d.length - 1 : d.length;
      if (score(c.domain) < score(existing.domain)) {
        dedup.set(c.name, c);
      } else {
        dedup.set(c.name, c);
      }
    }

    return Array.from(dedup.values());
  }
}
