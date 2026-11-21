import type { DccState } from "../api/types";

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function objectId(type: string, instance: number): string {
  return `${type}:${instance}`;
}

const ACRONYMS: Record<string, string> = { cov: "COV", utc: "UTC", apdu: "APDU", bbmd: "BBMD" };

export function words(kebab: string): string {
  const text = kebab
    .split("-")
    .map((w) => ACRONYMS[w] ?? w)
    .join(" ")
    .replace(/\bmulti state\b/g, "multi-state");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const DCC: Record<DccState, string> = {
  enable: "Communication enabled",
  disable: "Communication disabled",
  disableInitiation: "Initiation disabled",
};

export function dccText(state: DccState): string {
  return DCC[state];
}

const SEGMENTATION: Record<string, string> = {
  "segmented-both": "Both directions",
  "segmented-transmit": "Transmit only",
  "segmented-receive": "Receive only",
  "no-segmentation": "None",
};

export function segmentationText(value: string): string {
  return SEGMENTATION[value] ?? words(value);
}
