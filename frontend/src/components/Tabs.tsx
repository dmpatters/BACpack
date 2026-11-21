import { createContext, useContext, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

export type TabDef = { slug: string; label: string };

// Tabs are links, so reload and Back keep the section. The first tab is the
// bare page URL.
export default function Tabs({
  tabs,
  active,
  basePath,
  label,
  replace,
  state,
}: {
  tabs: TabDef[];
  active: string;
  basePath: string;
  label: string;
  // Tabs that shouldn't add history entries (an overlay's Back closes it).
  replace?: boolean;
  state?: unknown;
}) {
  return (
    <nav className="bp-tabs" aria-label={label}>
      {tabs.map((tab, i) => (
        <Link
          key={tab.slug}
          className="bp-tab"
          to={i === 0 ? basePath : `${basePath}/${tab.slug}`}
          replace={replace}
          state={state}
          aria-current={tab.slug === active ? "page" : undefined}
        >
          {tab.label}
        </Link>
      ))}
    </nav>
  );
}

const TabShown = createContext(true);

// Whether the panel this renders in is the shown tab (true outside TabPanels),
// so a hidden tab can stop its clocks and polling.
export function useTabShown(): boolean {
  return useContext(TabShown);
}

// Renders a tab once it's been opened and keeps it mounted, hidden, after
// that, so a tab's unsaved edits survive switching to another tab and back.
// This relies on the page staying mounted across tab URLs: each page has one
// route with an optional :tab segment.
export function TabPanels({
  active,
  panels,
}: {
  active: string;
  panels: Array<{ key: string; render: () => ReactNode }>;
}) {
  const [opened, setOpened] = useState<ReadonlySet<string>>(() => new Set());
  if (panels.some((p) => p.key === active) && !opened.has(active)) {
    setOpened(new Set(opened).add(active));
  }
  return (
    <>
      {panels.map((panel) =>
        panel.key === active || opened.has(panel.key) ? (
          <div key={panel.key} hidden={panel.key !== active}>
            <TabShown.Provider value={panel.key === active}>{panel.render()}</TabShown.Provider>
          </div>
        ) : null
      )}
    </>
  );
}
