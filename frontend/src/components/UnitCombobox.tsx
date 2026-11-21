import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { unitsApi } from "../api/endpoints";
import Combobox from "./Combobox";
import { LoadError } from "./Banner";
import { unitOptions } from "../utils/units";

// The units picker for single and bulk edit alike; "" means no units.
export default function UnitCombobox({
  value,
  ...props
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
}) {
  const units = useQuery({ queryKey: ["units"], queryFn: () => unitsApi.list().then((r) => r.units) });
  const options = useMemo(() => unitOptions(units.data, value), [units.data, value]);
  if (units.isError) return <LoadError what="the units" error={units.error} onRetry={() => units.refetch()} />;
  return <Combobox {...props} options={options} value={value} emptyText={(q) => `No units match "${q}"`} />;
}
