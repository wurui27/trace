import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const appDirectory = fileURLToPath(new URL("../app", import.meta.url));

function tsxFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = `${directory}/${entry.name}`;
    if (entry.isDirectory()) return tsxFiles(path);
    return entry.isFile() && entry.name.endsWith(".tsx") ? [path] : [];
  });
}

describe("vinext navigation compatibility", () => {
  it("disables unsupported RSC prefetching on every Next Link", () => {
    const linkFiles = tsxFiles(appDirectory).filter((path) =>
      readFileSync(path, "utf8").includes('from "next/link"'),
    );

    expect(linkFiles.length).toBeGreaterThan(0);
    for (const path of linkFiles) {
      const source = readFileSync(path, "utf8");
      const linkTags = source.match(/<Link\b[^>]*>/gs) ?? [];
      expect(linkTags.length, path).toBeGreaterThan(0);
      for (const tag of linkTags) {
        expect(tag, path).toContain("prefetch={false}");
      }
    }
  });
});
