// Native date/time inputs display in the browser's locale (MM/DD/YYYY,
// 12-hour on a US machine); this repeats the value in ISO order and
// 24-hour time, with its zone, so what is sent is never ambiguous.
const PATTERNS = {
  date: /^\d{4}-\d{2}-\d{2}$/,
  time: /^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$/,
  datetime: /^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$/,
};

export function isoText(value: string, kind: keyof typeof PATTERNS): string | null {
  return PATTERNS[kind].test(value) ? value.replace("T", " ") : null;
}

export default function IsoEcho({ value, kind, zone }: { value: string; kind: keyof typeof PATTERNS; zone?: string }) {
  const text = isoText(value, kind);
  if (text === null) return null;
  return (
    <span className="bp-help bp-mono" data-testid="iso-echo">
      {zone ? `${text} ${zone}` : text}
    </span>
  );
}
