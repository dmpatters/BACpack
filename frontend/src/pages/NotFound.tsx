import { useLocation } from "react-router-dom";
import EmptyState from "../components/EmptyState";
import { usePageTitle } from "../utils/usePageTitle";

export default function NotFound() {
  const { pathname } = useLocation();
  usePageTitle("Page not found");
  return (
    <EmptyState
      title="Page not found"
      text={`Nothing is at ${pathname}.`}
      backTo="/"
      backLabel="Back to devices"
    />
  );
}
