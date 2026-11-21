import Callout from "../components/Callout";
import LogView from "../components/LogView";
import PageHeader from "../components/PageHeader";
import { usePageTitle } from "../utils/usePageTitle";

export default function Logs() {
  usePageTitle("Logs");
  return (
    <>
      <PageHeader title="Logs" />
      <Callout tone="info">
        Server-wide log messages. Refreshes every few seconds unless paused.
      </Callout>
      <LogView />
    </>
  );
}
