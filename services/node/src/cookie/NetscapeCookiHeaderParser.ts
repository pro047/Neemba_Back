import { promises as fs } from "fs";
import path from "path";
import z from "zod";

const NetscapeCookie = z.object({
  domain: z.string().min(1),
  includeSubdomains: z.boolean(),
  path: z.string().min(1),
  isSecure: z.boolean(),
  expiresAt: z.number().nonnegative(),
  name: z.string().min(1),
  value: z.string().min(1),
});

export type NetscapeCookie = z.infer<typeof NetscapeCookie>;

export class NetscapeCookieHeaderParser {
  constructor(private cookieFilePath: string) {}

  async parse(): Promise<NetscapeCookie[]> {
    const absolutePath = path.resolve(this.cookieFilePath);
    const fileContent = await fs.readFile(absolutePath, "utf-8");

    const cookies: NetscapeCookie[] = [];
    for (const rawLine of fileContent.split("\n")) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) continue;

      const columns = line.split("\t");
      if (columns.length < 7) continue;

      const [
        domain,
        includeSubdomainsStr,
        cookiePath,
        isSecureStr,
        expiresAtStr,
        cookieName,
        cookieValue,
      ] = columns;

      if (!domain || !cookiePath || !cookieName || !cookieValue) continue;

      cookies.push({
        domain,
        includeSubdomains: includeSubdomainsStr?.toUpperCase() === "TRUE",
        path: cookiePath,
        isSecure: isSecureStr?.toUpperCase() === "TRUE",
        expiresAt: Number(expiresAtStr) || 0,
        name: cookieName,
        value: cookieValue,
      });
    }
    return cookies;
  }
}
