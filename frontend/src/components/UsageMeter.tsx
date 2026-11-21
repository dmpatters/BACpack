import { useId, type CSSProperties } from "react";

export function meterTone(percent: number): "accent" | "warn" | "danger" {
  if (percent > 95) return "danger";
  if (percent > 80) return "warn";
  return "accent";
}

export default function UsageMeter({
  label,
  valueText,
  percent,
  small = false,
  warns = true,
}: {
  label: string;
  valueText: string;
  percent: number;
  small?: boolean;
  /** False for a meter where full is normal, so it never turns amber or red. */
  warns?: boolean;
}) {
  const labelId = useId();
  const clamped = Math.max(0, Math.min(100, percent));
  const tone = warns ? meterTone(clamped) : "accent";
  return (
    <div
      className={small ? "bp-meter bp-meter--sm" : "bp-meter"}
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped)}
      aria-valuetext={valueText}
      aria-label={small ? label : undefined}
      aria-labelledby={small ? undefined : labelId}
    >
      {!small && (
        <div className="bp-meter__head">
          <span id={labelId}>{label}</span>
          <span className="bp-mono">{clamped.toFixed(1)}%</span>
        </div>
      )}
      <div className="bp-meter__track">
        <div
          className={tone === "accent" ? "bp-meter__fill" : `bp-meter__fill bp-meter__fill--${tone}`}
          style={{ "--value": `${clamped}%` } as CSSProperties}
        />
      </div>
    </div>
  );
}
