import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { buttonClass } from "./Button";

// Not-found and couldn't-load states; level 2 when it sits under a page header.
export default function EmptyState({
  title,
  text,
  backTo,
  backLabel,
  level = 1,
  replace,
}: {
  title: string;
  text?: ReactNode;
  backTo: string;
  backLabel: string;
  level?: 1 | 2;
  // For a state inside an overlay, where a pushed entry would make Back reopen it.
  replace?: boolean;
}) {
  const Heading = level === 1 ? "h1" : "h2";
  return (
    <div className="bp-empty">
      <Heading className="bp-empty__title">{title}</Heading>
      {text && <p className="bp-empty__text">{text}</p>}
      <Link className={buttonClass()} to={backTo} replace={replace}>
        {backLabel}
      </Link>
    </div>
  );
}
