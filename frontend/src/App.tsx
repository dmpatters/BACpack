import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import DeviceDetail from "./pages/DeviceDetail";
import ConnectorsList from "./pages/ConnectorsList";
import ConnectorPage from "./pages/ConnectorPage";
import NetworkSettings from "./pages/NetworkSettings";
import TrendStorage from "./pages/TrendStorage";
import Logs from "./pages/Logs";
import Config from "./pages/Config";
import About from "./pages/About";
import ApiDocs from "./pages/ApiDocs";
import NotFound from "./pages/NotFound";
import { LeaveGuard, UnsavedChangesProvider } from "./components/UnsavedChanges";
import AuthGate from "./components/AuthGate";
import Security from "./pages/Security";
import WebServer from "./pages/WebServer";

// The data router's single element: the guard needs the data router, the
// routes themselves stay declared in App. AuthGate holds the app back until
// the install has a login choice and, when one is required, a session.
export function Root() {
  return (
    <UnsavedChangesProvider>
      <AuthGate>
        <App />
      </AuthGate>
      <LeaveGuard />
    </UnsavedChangesProvider>
  );
}

// Keep these paths stable: they are bookmarked and linked from outside the
// SPA. Each detail page has one route with an optional :tab, so switching tabs
// doesn't remount the page (TabPanels keeps unsaved edits that way). An
// object's URL is the device page with that object's overlay open.
export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/devices" element={<Dashboard />} />
        {/* The Devices page with its New device or Import EDE panel open. */}
        <Route path="/devices/new" element={<Dashboard />} />
        <Route path="/devices/import-ede" element={<Dashboard />} />
        <Route path="/devices/sample" element={<Dashboard />} />
        <Route path="/devices/:deviceId/:tab?" element={<DeviceDetail />} />
        {/* The same element as the route above, so the device page and its list stay mounted under an object's overlay. */}
        <Route path="/devices/:deviceId/objects/:objectId/:tab?" element={<DeviceDetail />} />
        <Route path="/connectors" element={<ConnectorsList />} />
        <Route path="/connectors/:connectorId" element={<ConnectorPage />} />
        <Route path="/settings/network" element={<NetworkSettings />} />
        <Route path="/settings/security" element={<Security />} />
        <Route path="/settings/web" element={<WebServer />} />
        <Route path="/trends" element={<TrendStorage />} />
        <Route path="/logs" element={<Logs />} />
        <Route path="/config" element={<Config />} />
        <Route path="/about" element={<About />} />
        <Route path="/docs" element={<ApiDocs />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Layout>
  );
}
