import type { ReactNode } from "react";

export default function PageHeader({
  title,
  meta,
  sub,
  actions,
}: {
  title: ReactNode;
  meta?: ReactNode;
  sub?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="bp-page-header">
      <div>
        <h1 className="bp-page-header__title">
          {title}
          {meta && <span className="bp-page-header__meta">{meta}</span>}
        </h1>
        {sub && <p className="bp-page-header__sub">{sub}</p>}
      </div>
      {actions && <div className="bp-cluster">{actions}</div>}
    </header>
  );
}
