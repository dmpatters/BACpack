import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { connectorsApi } from "../api/endpoints";
import { isNotFound } from "../api/http";
import { errorMessage } from "../components/Banner";
import EmptyState from "../components/EmptyState";
import { Loading } from "../components/AsyncState";
import BacnetConnectorDetail from "./BacnetConnectorDetail";
import ConnectorDetail from "./ConnectorDetail";
import { parseId } from "../utils/ids";
import { usePageTitle } from "../utils/usePageTitle";

// /connectors/:connectorId -- the detail page for the connector's type. The
// HTTP page fetches the connector itself too; the shared query key means
// that's served from this query's cache.
export default function ConnectorPage() {
  const { connectorId } = useParams<{ connectorId: string }>();
  const id = parseId(connectorId);
  const { data: connector, error } = useQuery({
    queryKey: ["connector", id],
    queryFn: () => connectorsApi.get(id!),
    enabled: id !== null,
  });
  const problem =
    id === null || (error && !connector && isNotFound(error))
      ? `Connector ${connectorId} not found`
      : error && !connector
        ? `Couldn't load connector ${connectorId}`
        : null;
  // Titles both detail pages: a child's effect runs before its parent's, so
  // a title set inside them would be overwritten here.
  usePageTitle(...(problem ? [problem] : [connector?.name, "Connectors"]));
  if (problem) {
    return (
      <EmptyState
        title={problem}
        text={
          error && !isNotFound(error) ? errorMessage(error) : undefined
        }
        backTo="/connectors"
        backLabel="Back to connectors"
      />
    );
  }
  if (!connector) return <Loading what="connector" />;
  return connector.type === "bacnet" ? <BacnetConnectorDetail connector={connector} /> : <ConnectorDetail />;
}
