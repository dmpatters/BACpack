import { forwardRef, type ButtonHTMLAttributes } from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "danger-solid" | "delete";

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; small?: boolean };

export function buttonClass(variant: ButtonVariant = "secondary", small = false): string {
  const classes = ["bp-btn"];
  if (variant !== "secondary") classes.push(`bp-btn--${variant}`);
  if (small) classes.push("bp-btn--sm");
  return classes.join(" ");
}

// No default `type`: a button inside a form still submits it, as a plain
// <button> does.
const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", small = false, className, ...props },
  ref
) {
  const classes = buttonClass(variant, small);
  return <button ref={ref} {...props} className={className ? `${classes} ${className}` : classes} />;
});

export default Button;
