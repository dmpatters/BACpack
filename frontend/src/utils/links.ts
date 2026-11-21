export const REPO_URL = "https://github.com/dmpatters/BACpack";

// A guide under docs/guide, as "file.md" or "file.md#anchor".
export function guideUrl(path: string): string {
  return `${REPO_URL}/blob/main/docs/guide/${path}`;
}

export type SkippedTypesSource = "clone" | "ede";

const SOURCE_WORDS: Record<SkippedTypesSource, { title: string; lead: string }> = {
  clone: { title: "clones", lead: "Cloning a controller" },
  ede: { title: "EDE imports", lead: "Importing an EDE file" },
};

// A pre-filled new-issue page asking for the object types a clone or an EDE
// import skipped. Only types, counts, the vendor and the version: object
// names, addresses and device ids are a site's details and stay off the URL.
export function featureRequestUrl(
  skippedTypes: Record<string, number>,
  vendorName: string | null,
  version: string,
  source: SkippedTypesSource = "clone"
): string {
  const types = Object.keys(skippedTypes).sort();
  const words = SOURCE_WORDS[source];
  const title = `Support for ${types.join(", ")} objects in ${words.title}`;
  const body = [
    `${words.lead} skipped these object types:`,
    "",
    ...types.map((type) => `- ${type}: ${skippedTypes[type]}`),
    "",
    `Vendor: ${vendorName?.trim() || "not given"}`,
    `BACpack version: ${version}`,
  ].join("\n");
  const query = new URLSearchParams({ title, body });
  return `${REPO_URL}/issues/new?${query.toString().replace(/\+/g, "%20")}`;
}
