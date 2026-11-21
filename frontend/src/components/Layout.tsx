import { useRef, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { versionApi } from "../api/endpoints";
import { LayersProvider } from "./layers";
import ThemeToggle from "./ThemeToggle";
import Button from "./Button";
import { useSignOut } from "./AuthGate";
import { useAuthState } from "../utils/useAuthState";

export const NAV_LINKS: Array<{ label: string; to: string }> = [
  { label: "Devices", to: "/" },
  { label: "Connectors", to: "/connectors" },
  { label: "Trend Storage", to: "/trends" },
  { label: "Logs", to: "/logs" },
  { label: "Network Settings", to: "/settings/network" },
  { label: "Security", to: "/settings/security" },
  { label: "Web Server", to: "/settings/web" },
  { label: "Config", to: "/config" },
  { label: "API Docs", to: "/docs" },
  { label: "About", to: "/about" },
];

// The device list is the home page, so "/" and everything under /devices
// belong to Devices.
function isCurrent(to: string, pathname: string): boolean {
  if (to === "/") return pathname === "/" || pathname === "/devices" || pathname.startsWith("/devices/");
  return pathname === to || pathname.startsWith(`${to}/`);
}

export default function Layout({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const { data: version } = useQuery({
    queryKey: ["version"],
    queryFn: versionApi.get,
  });

  const { data: auth } = useAuthState();
  const signOut = useSignOut();

  const nav = useRef<HTMLElement>(null);
  const main = useRef<HTMLElement>(null);

  return (
    <LayersProvider shell={() => [nav.current, main.current].filter((el): el is HTMLElement => el !== null)}>
      <nav ref={nav} className="bp-nav bp-nav--sticky" aria-label="Main">
        <Link className="bp-nav__brand" to="/">
          BACpack
        </Link>
        <div className="bp-nav__links">
          {NAV_LINKS.map(({ label, to }) => (
            <Link
              key={to}
              className="bp-nav__link"
              to={to}
              aria-current={isCurrent(to, pathname) ? "page" : undefined}
            >
              {label}
            </Link>
          ))}
        </div>
        <ThemeToggle />
        {auth?.mode === "login" && auth.signed_in && (
          <Button small variant="ghost" type="button" onClick={() => void signOut()}>
            Sign out
          </Button>
        )}
        {version && <span className="bp-nav__version">v{version.version}</span>}
      </nav>
      <main ref={main} className="bp-page" tabIndex={-1}>
        {children}
      </main>
    </LayersProvider>
  );
}
