import { CookieFilter } from "./CookieFilter";
import { CookieHeaderBuilder } from "./CookieHeaderBuilder";
import { NetscapeCookieHeaderParser } from "./NetscapeCookiHeaderParser";

export class CookieHeaderProvider {
  constructor(private cookieFilePath: string) {}

  async getCookieHeaderFor(requestUrl: string): Promise<string> {
    const allCookies = await new NetscapeCookieHeaderParser(
      this.cookieFilePath
    ).parse();
    const filtered = new CookieFilter({ requestUrl }).filter(allCookies);
    if (filtered.length === 0) {
      throw new Error("No usable cookies for this URL");
    }
    return CookieHeaderBuilder.build(filtered);
  }
}
